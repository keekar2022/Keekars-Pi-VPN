#!/usr/bin/env python3
# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# Minimal always-root helper daemon brokering privileged WireGuard
# operations for the unprivileged pi-config-ui service, over a local Unix
# socket. Mirrors the shape of NetworkManager+polkit brokering nmcli's
# privileged actions today (see deploy/polkit-rules/), since WireGuard has
# no D-Bus service or polkit action IDs of its own to reuse.
#
# Protocol: one newline-delimited JSON object per request, one per
# response, one request per connection. Every request has a "cmd" field;
# every response has "ok": bool plus "result" or "error".
#
# This daemon is deliberately policy-free: it validates that requests are
# well-formed and safe to execute (name/key patterns, no shell injection
# surface) but makes no judgment calls (e.g. the AllowedIPs-overlap safety
# check lives in the FastAPI app, not here) — keeping this process's own
# attack surface as small as possible since it runs as root.

import asyncio
import grp
import ipaddress
import json
import logging
import os
import pwd
import re
import socket
import struct
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","service.name":"pi-wg-helperd","message":"%(message)s"}',
)
logger = logging.getLogger("pi_wg_helperd")

SOCKET_PATH = "/run/pi-wg-helperd/helper.sock"
SOCKET_GROUP = "pi-wg-helper"
ALLOWED_CALLER_USER = "pi-config-ui"
WIREGUARD_DIR = Path("/etc/wireguard")

TUNNEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,15}$")
# Standard 32-byte value, base64-encoded (44 chars incl. one "=" pad char).
# The character immediately before the pad is constrained to a 16-value
# subset because only 16 of 18 encoded bits in the final group are real
# data — this rejects malformed/injected values, not just wrong-length ones.
WG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")


class HelperError(Exception):
    pass


def _validate_tunnel_name(name) -> str:
    if not isinstance(name, str) or not TUNNEL_NAME_PATTERN.match(name):
        raise HelperError("invalid tunnel name")
    return name


def _validate_wg_key(key, field: str) -> str:
    if not isinstance(key, str) or not WG_KEY_PATTERN.match(key):
        raise HelperError(f"invalid {field}")
    return key


def _validate_allowed_ips(allowed_ips) -> str:
    if not isinstance(allowed_ips, list) or not allowed_ips:
        raise HelperError("allowed_ips must be a non-empty list")
    nets = []
    for entry in allowed_ips:
        try:
            nets.append(str(ipaddress.ip_network(entry, strict=False)))
        except (ValueError, TypeError):
            raise HelperError(f"invalid allowed_ips entry: {entry!r}")
    return ", ".join(nets)


def _tunnel_conf_path(name: str) -> Path:
    return WIREGUARD_DIR / f"{name}.conf"


def _split_conf(content: str) -> tuple[str, list[str]]:
    parts = re.split(r"\n(?=\[Peer\])", content)
    return parts[0], parts[1:]


def _rebuild_conf(header: str, peer_blocks: list[str]) -> str:
    out = [header.rstrip("\n") + "\n"]
    for block in peer_blocks:
        out.append("\n" + block.rstrip("\n") + "\n")
    return "".join(out)


def _read_conf(name: str) -> str:
    path = _tunnel_conf_path(name)
    if not path.exists():
        raise HelperError("tunnel not found")
    return path.read_text()


def _write_conf(name: str, content: str) -> None:
    path = _tunnel_conf_path(name)
    tmp = path.with_suffix(".conf.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _is_interface_up_sync(name: str) -> bool:
    return Path(f"/sys/class/net/{name}").exists()


_ADDRESS_LINE = re.compile(r"^\s*Address\s*=\s*(.+?)\s*$", re.MULTILINE)
_ENDPOINT_LINE = re.compile(r"^\s*Endpoint\s*=\s*(.+?)\s*$", re.MULTILINE)
_ALLOWED_IPS_LINE = re.compile(r"^\s*AllowedIPs\s*=\s*(.+?)\s*$", re.MULTILINE)


def _parse_conf_info(content: str) -> dict:
    """Derive role/routing info directly from a tunnel's .conf text, so it's
    available even when the interface is currently down (unlike `wg show`,
    which only reports live/up interfaces) — needed for the pre-connect
    AllowedIPs-overlap safety check in app/wireguard.py.

    mode is "client" if any [Peer] block has an Endpoint (a road-warrior's
    peer entry always dials a fixed address; a server's peer entries for
    road-warriors never do) — standard WireGuard convention, not specific
    to this project.
    """
    header, peer_blocks = _split_conf(content)
    address_match = _ADDRESS_LINE.search(header)
    endpoint = None
    allowed_ips: list[str] = []
    for block in peer_blocks:
        m = _ENDPOINT_LINE.search(block)
        if m and endpoint is None:
            endpoint = m.group(1)
        for ips_match in _ALLOWED_IPS_LINE.finditer(block):
            allowed_ips.extend(part.strip() for part in ips_match.group(1).split(",") if part.strip())
    return {
        "mode": "client" if endpoint else "server",
        "configured_address": address_match.group(1) if address_match else None,
        "configured_endpoint": endpoint,
        "configured_allowed_ips": allowed_ips,
    }


async def _run(*args: str, input_data: bytes | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=input_data)
    return proc.returncode, stdout, stderr


async def cmd_genkey(_req: dict) -> dict:
    rc, priv, err = await _run("wg", "genkey")
    if rc != 0:
        raise HelperError(f"wg genkey failed: {err.decode(errors='replace')}")
    rc, pub, err = await _run("wg", "pubkey", input_data=priv)
    if rc != 0:
        raise HelperError(f"wg pubkey failed: {err.decode(errors='replace')}")
    return {"private_key": priv.decode().strip(), "public_key": pub.decode().strip()}


async def cmd_create_tunnel(req: dict) -> dict:
    name = _validate_tunnel_name(req.get("name"))
    private_key = _validate_wg_key(req.get("private_key"), "private_key")
    try:
        address = ipaddress.ip_interface(req.get("address", ""))
    except ValueError:
        raise HelperError("invalid address")
    listen_port = req.get("listen_port")
    if not isinstance(listen_port, int) or not (1024 <= listen_port <= 65535):
        raise HelperError("invalid listen_port")

    path = _tunnel_conf_path(name)
    if path.exists():
        raise HelperError("tunnel already exists")

    WIREGUARD_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(WIREGUARD_DIR, 0o700)
    content = f"[Interface]\nPrivateKey = {private_key}\nAddress = {address}\nListenPort = {listen_port}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {}


async def cmd_delete_tunnel(req: dict) -> dict:
    name = _validate_tunnel_name(req.get("name"))
    await _run("systemctl", "disable", "--now", f"wg-quick@{name}")
    _tunnel_conf_path(name).unlink(missing_ok=True)
    return {}


async def cmd_add_peer(req: dict) -> dict:
    tunnel = _validate_tunnel_name(req.get("tunnel"))
    public_key = _validate_wg_key(req.get("public_key"), "public_key")
    allowed_ips = _validate_allowed_ips(req.get("allowed_ips"))
    preshared_key = req.get("preshared_key")
    if preshared_key is not None:
        _validate_wg_key(preshared_key, "preshared_key")
    keepalive = req.get("keepalive")
    if keepalive is not None and not (isinstance(keepalive, int) and 0 <= keepalive <= 3600):
        raise HelperError("invalid keepalive")

    header, peer_blocks = _split_conf(_read_conf(tunnel))
    if any(f"PublicKey = {public_key}" in b for b in peer_blocks):
        raise HelperError("peer already exists")

    block = f"[Peer]\nPublicKey = {public_key}\n"
    if preshared_key:
        block += f"PresharedKey = {preshared_key}\n"
    block += f"AllowedIPs = {allowed_ips}\n"
    if keepalive:
        block += f"PersistentKeepalive = {keepalive}\n"
    _write_conf(tunnel, _rebuild_conf(header, peer_blocks + [block]))

    if _is_interface_up_sync(tunnel):
        args = ["wg", "set", tunnel, "peer", public_key, "allowed-ips", allowed_ips]
        if keepalive:
            args += ["persistent-keepalive", str(keepalive)]
        psk_tmp = None
        try:
            if preshared_key:
                psk_dir = Path(SOCKET_PATH).parent
                psk_tmp = psk_dir / f"psk-{os.getpid()}-{id(req)}.tmp"
                fd = os.open(psk_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(preshared_key + "\n")
                args += ["preshared-key", str(psk_tmp)]
            rc, _, err = await _run(*args)
            if rc != 0:
                raise HelperError(f"wg set failed: {err.decode(errors='replace')}")
        finally:
            if psk_tmp is not None:
                psk_tmp.unlink(missing_ok=True)
    return {}


async def cmd_remove_peer(req: dict) -> dict:
    tunnel = _validate_tunnel_name(req.get("tunnel"))
    public_key = _validate_wg_key(req.get("public_key"), "public_key")

    header, peer_blocks = _split_conf(_read_conf(tunnel))
    kept = [b for b in peer_blocks if f"PublicKey = {public_key}" not in b]
    _write_conf(tunnel, _rebuild_conf(header, kept))

    if _is_interface_up_sync(tunnel):
        rc, _, err = await _run("wg", "set", tunnel, "peer", public_key, "remove")
        if rc != 0:
            raise HelperError(f"wg set remove failed: {err.decode(errors='replace')}")
    return {}


async def cmd_tunnel_status(req: dict) -> dict:
    name = req.get("name")
    if name is not None:
        _validate_tunnel_name(name)

    conf_names = sorted(p.stem for p in WIREGUARD_DIR.glob("*.conf")) if WIREGUARD_DIR.exists() else []
    if name is not None:
        conf_names = [n for n in conf_names if n == name]

    rc, dump_out, _ = await _run("wg", "show", "all", "dump")
    live: dict[str, dict] = {}
    if rc == 0:
        for line in dump_out.decode(errors="replace").splitlines():
            cols = line.split("\t")
            if not cols or not cols[0]:
                continue
            entry = live.setdefault(cols[0], {"peers": []})
            if len(cols) == 5:
                entry["public_key"] = cols[2]
                entry["listen_port"] = cols[3]
            elif len(cols) == 9:
                entry["peers"].append(
                    {
                        "public_key": cols[1],
                        "endpoint": cols[3] if cols[3] != "(none)" else None,
                        "allowed_ips": cols[4],
                        "latest_handshake": int(cols[5]) if cols[5].isdigit() else 0,
                        "rx_bytes": int(cols[6]) if cols[6].isdigit() else 0,
                        "tx_bytes": int(cols[7]) if cols[7].isdigit() else 0,
                    }
                )

    tunnels = []
    for tname in conf_names:
        rc, out, _ = await _run("systemctl", "is-active", f"wg-quick@{tname}")
        info = live.get(tname, {})
        try:
            conf_info = _parse_conf_info(_read_conf(tname))
        except HelperError:
            conf_info = {"mode": "server", "configured_address": None, "configured_endpoint": None, "configured_allowed_ips": []}
        tunnels.append(
            {
                "name": tname,
                "active": out.decode().strip() == "active",
                "listen_port": info.get("listen_port"),
                "public_key": info.get("public_key"),
                "peers": info.get("peers", []),
                **conf_info,
            }
        )
    return {"tunnels": tunnels}


async def cmd_set_tunnel_state(req: dict) -> dict:
    name = _validate_tunnel_name(req.get("name"))
    action = req.get("action")
    if action not in ("activate", "deactivate", "disconnect"):
        raise HelperError("invalid action")
    if not _tunnel_conf_path(name).exists():
        raise HelperError("tunnel not found")
    if action == "activate":
        # enable + start in one call: guarantees both "up right now" and
        # "auto-starts on every future boot" — the latter must never be
        # undone by a mere disconnect (see "disconnect" below).
        rc, _, err = await _run("systemctl", "enable", "--now", f"wg-quick@{name}")
    elif action == "disconnect":
        # Plain stop — deliberately does NOT disable. A user disconnecting a
        # client tunnel right now must not change its enabled-at-boot state;
        # the next boot should still auto-reconnect.
        rc, _, err = await _run("systemctl", "stop", f"wg-quick@{name}")
    else:
        rc, _, err = await _run("systemctl", "disable", "--now", f"wg-quick@{name}")
    if rc != 0:
        raise HelperError(f"systemctl failed: {err.decode(errors='replace')}")
    return {}


DISPATCH = {
    "genkey": cmd_genkey,
    "create_tunnel": cmd_create_tunnel,
    "delete_tunnel": cmd_delete_tunnel,
    "add_peer": cmd_add_peer,
    "remove_peer": cmd_remove_peer,
    "tunnel_status": cmd_tunnel_status,
    "set_tunnel_state": cmd_set_tunnel_state,
}


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    sock = writer.get_extra_info("socket")
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        try:
            allowed_uid = pwd.getpwnam(ALLOWED_CALLER_USER).pw_uid
        except KeyError:
            logger.error("caller_user_missing", extra={"event": "helper.startup.caller_user_missing"})
            return
        if _peer_uid(writer) != allowed_uid:
            logger.warning("rejected_peer", extra={"event": "helper.auth.rejected"})
            return

        line = await reader.readline()
        if not line:
            return
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            writer.write((json.dumps({"ok": False, "error": "invalid request"}) + "\n").encode())
            await writer.drain()
            return

        cmd = req.get("cmd")
        handler = DISPATCH.get(cmd)
        if handler is None:
            writer.write((json.dumps({"ok": False, "error": "unknown command"}) + "\n").encode())
            await writer.drain()
            return

        try:
            result = await handler(req)
            writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
        except HelperError as exc:
            logger.warning("command_rejected", extra={"event": "helper.command.rejected", "cmd": cmd, "error": str(exc)})
            writer.write((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
        except Exception as exc:  # noqa: BLE001 - must never crash the daemon on a bad request
            logger.error("command_failed", extra={"event": "helper.command.failed", "cmd": cmd, "error": str(exc)})
            writer.write((json.dumps({"ok": False, "error": "internal error"}) + "\n").encode())
        await writer.drain()
    finally:
        writer.close()


async def main() -> None:
    socket_dir = Path(SOCKET_PATH).parent
    socket_dir.mkdir(parents=True, exist_ok=True)
    if Path(SOCKET_PATH).exists():
        Path(SOCKET_PATH).unlink()

    server = await asyncio.start_unix_server(handle_client, path=SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)
    try:
        gid = grp.getgrnam(SOCKET_GROUP).gr_gid
        os.chown(SOCKET_PATH, 0, gid)
    except KeyError:
        logger.warning(
            "group_missing",
            extra={"event": "helper.startup.group_missing", "group": SOCKET_GROUP},
        )

    logger.info("started", extra={"event": "helper.startup", "socket": SOCKET_PATH})
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
