# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# Thin client for the pi-wg-helperd Unix socket (deploy/pi-wg-helperd/helper.py).
# Matches the abstraction level of app/network.py's _run_nmcli: one function
# per privileged operation, generic error to the caller, full detail logged
# server-side only. Never log request/response payloads wholesale — some
# carry private keys (see app/wireguard.py's key-handling invariant).

import asyncio
import json
import logging

from fastapi import HTTPException, status

logger = logging.getLogger("pi_config_ui.wg_helper_client")

SOCKET_PATH = "/run/pi-wg-helperd/helper.sock"


async def _call(cmd: str, **params) -> dict:
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
    except OSError as exc:
        logger.error(
            "helper_unreachable",
            extra={"event": "wireguard.helper.unreachable", "cmd": cmd, "error": str(exc)},
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="WireGuard helper unavailable")

    try:
        writer.write((json.dumps({"cmd": cmd, **params}) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()

    if not line:
        logger.error("helper_empty_response", extra={"event": "wireguard.helper.empty_response", "cmd": cmd})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="WireGuard operation failed")

    try:
        response = json.loads(line)
    except json.JSONDecodeError:
        logger.error("helper_bad_response", extra={"event": "wireguard.helper.bad_response", "cmd": cmd})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="WireGuard operation failed")

    if not response.get("ok"):
        logger.error(
            "helper_command_failed",
            extra={"event": "wireguard.helper.failed", "cmd": cmd, "error": response.get("error")},
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="WireGuard operation failed")

    return response.get("result") or {}


async def genkey() -> dict:
    return await _call("genkey")


async def create_tunnel(name: str, address: str, listen_port: int, private_key: str) -> None:
    await _call("create_tunnel", name=name, address=address, listen_port=listen_port, private_key=private_key)


async def delete_tunnel(name: str) -> None:
    await _call("delete_tunnel", name=name)


async def add_peer(
    tunnel: str,
    public_key: str,
    allowed_ips: list[str],
    preshared_key: str | None = None,
    keepalive: int | None = None,
) -> None:
    await _call(
        "add_peer",
        tunnel=tunnel,
        public_key=public_key,
        allowed_ips=allowed_ips,
        preshared_key=preshared_key,
        keepalive=keepalive,
    )


async def remove_peer(tunnel: str, public_key: str) -> None:
    await _call("remove_peer", tunnel=tunnel, public_key=public_key)


async def tunnel_status(name: str | None = None) -> dict:
    return await _call("tunnel_status", name=name)


async def set_tunnel_state(name: str, action: str) -> None:
    await _call("set_tunnel_state", name=name, action=action)
