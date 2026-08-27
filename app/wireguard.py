# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# HARD INVARIANT: a peer's private key exists only in this process's memory
# for the duration of the single request/response cycle that creates it
# (create_peer, below) — it's embedded directly into the downloaded .conf
# response body and is NEVER logged, NEVER written to wireguard-peers.json,
# and never persisted anywhere retrievable again. If a downloaded file is
# lost, the only recovery is deleting that peer and adding a new one. Same
# class of concern as the documented WiFi-PSK CLI-argument anti-pattern in
# app/network.py — don't let a WireGuard secret follow that path either.
#
# AllowedIPs has two different meanings depending on which side's config
# it's read from, and this module must not confuse them:
#   - the SERVER's [Peer] entry for a given peer must list that peer's own
#     tunnel address (source-IP filtering — the server drops any packet
#     whose source doesn't match), which is why peer tunnel addresses are
#     auto-assigned here rather than taken from the request; and
#   - the CLIENT's [Peer] entry (pointing at the server) lists the subnets
#     the operator wants that client to reach, which is exactly what the
#     add-peer form's `allowed_ips` field controls.

import ipaddress
import json
import logging
import uuid
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends, HTTPException, Response, status

from app import wg_helper_client as helper
from app.auth import current_user
from app.models import PeerCreateRequest, TunnelCreateRequest, TunnelStateRequest

logger = logging.getLogger("pi_config_ui.wireguard")

router = APIRouter(prefix="/api/wireguard", tags=["wireguard"], dependencies=[Depends(current_user)])

# Deliberately its own subdirectory, not /etc/pi-config-ui directly — that
# directory also holds sso.env and the TLS private key, and this feature's
# ReadWritePaths grant (deploy/pi-config-ui.service) should not extend to
# those.
_METADATA_PATH = Path("/etc/pi-config-ui/wireguard/peers.json")

_AF_INET = 2  # psutil doesn't re-export socket.AF_INET's int value directly


def _load_metadata() -> dict:
    if not _METADATA_PATH.exists():
        return {"tunnels": {}}
    try:
        data = json.loads(_METADATA_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("metadata_read_failed", extra={"event": "wireguard.metadata.read_failed", "error": str(exc)})
        return {"tunnels": {}}
    data.setdefault("tunnels", {})
    return data


def _save_metadata(data: dict) -> None:
    _METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _METADATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(_METADATA_PATH)


def _overlapping_local_subnets(candidate_nets: list[ipaddress.IPv4Network]) -> list[str]:
    """Subnets among candidate_nets that overlap a currently-connected local
    interface's directly-connected subnet — the exact failure mode that broke
    SSH to a Pi in this same project (see docs/PROJECT_NOTES.md, Part 3,
    "Incident 2: self-inflicted SSH lockout"). Interface-type-agnostic, so it
    also naturally covers any pre-existing wg-quick client interfaces.
    """
    conflicts = []
    for iface_name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family != _AF_INET or not addr.netmask:
                continue
            try:
                local_net = ipaddress.ip_interface(f"{addr.address}/{addr.netmask}").network
            except ValueError:
                continue
            for candidate in candidate_nets:
                if candidate.overlaps(local_net):
                    conflicts.append(f"{candidate} overlaps {iface_name} ({local_net})")
    return conflicts


def _allocate_peer_address(tunnel_meta: dict) -> str:
    iface = ipaddress.ip_interface(tunnel_meta["address"])
    used = {iface.ip} | {ipaddress.ip_interface(p["tunnel_address"]).ip for p in tunnel_meta["peers"]}
    for host in iface.network.hosts():
        if host not in used:
            return f"{host}/32"
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No free addresses left in this tunnel's subnet")


@router.get("/tunnels")
async def list_tunnels():
    status_result = await helper.tunnel_status()
    metadata = _load_metadata()
    tunnels = []
    for t in status_result.get("tunnels", []):
        mode = t.get("mode", "server")
        meta = metadata["tunnels"].get(t["name"], {})
        entry = {
            "name": t["name"],
            "mode": mode,
            "active": t["active"],
            "public_key": t.get("public_key"),
        }
        if mode == "client":
            # A client tunnel's own routing info lives in its .conf (parsed
            # by the helper), not in wireguard-peers.json — it was never
            # created through this UI's "create tunnel" flow, so there's no
            # metadata entry for it (e.g. the pre-existing Syd-Home tunnel).
            entry["remote_endpoint"] = t.get("configured_endpoint")
            entry["remote_allowed_ips"] = t.get("configured_allowed_ips", [])
            # "peers" is [] (not missing) whenever the tunnel is down — wg
            # show only reports live/up interfaces — so `or []` (not a
            # dict.get default, which only applies when the key is absent)
            # is what actually guards the [0] index below.
            live_peers = t.get("peers") or []
            live_peer = live_peers[0] if live_peers else {}
            entry["latest_handshake"] = live_peer.get("latest_handshake")
            entry["rx_bytes"] = live_peer.get("rx_bytes")
            entry["tx_bytes"] = live_peer.get("tx_bytes")
        else:
            entry["address"] = meta.get("address") or t.get("configured_address")
            entry["endpoint"] = meta.get("endpoint")
            entry["listen_port"] = t.get("listen_port")
            entry["peer_count"] = len(meta.get("peers", []))
        tunnels.append(entry)
    return tunnels


@router.post("/tunnels", status_code=status.HTTP_201_CREATED)
async def create_tunnel(payload: TunnelCreateRequest, user=Depends(current_user)):
    if not payload.force:
        conflicts = _overlapping_local_subnets([payload.address.network])
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This tunnel's own address overlaps a network this device is already "
                    "directly connected to, which can break local network access: "
                    + "; ".join(conflicts)
                    + ". Confirm you understand the risk to override."
                ),
            )

    keypair = await helper.genkey()
    await helper.create_tunnel(
        name=payload.name,
        address=str(payload.address),
        listen_port=payload.listen_port,
        private_key=keypair["private_key"],
    )

    metadata = _load_metadata()
    metadata["tunnels"][payload.name] = {
        "address": str(payload.address),
        "endpoint": payload.endpoint,
        "listen_port": payload.listen_port,
        "peers": [],
    }
    _save_metadata(metadata)
    logger.info(
        "tunnel_created",
        extra={"event": "wireguard.tunnel.created", "user": user.get("sub"), "tunnel": payload.name},
    )
    return {"name": payload.name, "public_key": keypair["public_key"]}


@router.delete("/tunnels/{name}")
async def delete_tunnel(name: str, user=Depends(current_user)):
    await helper.delete_tunnel(name)
    metadata = _load_metadata()
    metadata["tunnels"].pop(name, None)
    _save_metadata(metadata)
    logger.info("tunnel_deleted", extra={"event": "wireguard.tunnel.deleted", "user": user.get("sub"), "tunnel": name})
    return {"status": "ok"}


@router.post("/tunnels/{name}/state")
async def set_tunnel_state(name: str, payload: TunnelStateRequest, user=Depends(current_user)):
    # Existence + role/routing info come from the helper's conf-derived data,
    # not wireguard-peers.json — a tunnel that was never created through this
    # UI's "create tunnel" flow (e.g. the pre-existing Syd-Home client) has no
    # metadata entry, but must still be controllable here.
    status_result = await helper.tunnel_status(name)
    live_tunnels = status_result.get("tunnels", [])
    if not live_tunnels:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tunnel")
    tunnel_info = live_tunnels[0]
    mode = tunnel_info.get("mode", "server")

    if payload.action == "activate" and not payload.force:
        # Which subnets to check depends on role: a SERVER tunnel's own
        # interface address is the risk (it becomes a locally-connected
        # route once the interface is up); a CLIENT tunnel's [Peer]
        # AllowedIPs is the risk (those become routes on THIS device) — this
        # exact class of client-side overlap is what caused the real
        # incident on this project's Syd-Home tunnel.
        if mode == "client":
            candidates = []
            for entry in tunnel_info.get("configured_allowed_ips", []):
                try:
                    candidates.append(ipaddress.ip_network(entry, strict=False))
                except ValueError:
                    continue
        else:
            configured_address = tunnel_info.get("configured_address")
            candidates = [ipaddress.ip_interface(configured_address).network] if configured_address else []

        conflicts = _overlapping_local_subnets(candidates)
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Activating this tunnel would overlap a network this device is already "
                    "directly connected to, which can break local network access: "
                    + "; ".join(conflicts)
                    + ". Confirm you understand the risk to override."
                ),
            )

    await helper.set_tunnel_state(name, payload.action)
    logger.info(
        "tunnel_state_changed",
        extra={
            "event": "wireguard.tunnel.state",
            "user": user.get("sub"),
            "tunnel": name,
            "action": payload.action,
            "forced": payload.force,
        },
    )
    return {"status": "ok"}


@router.get("/tunnels/{name}/peers")
async def list_peers(name: str):
    metadata = _load_metadata()
    tunnel_meta = metadata["tunnels"].get(name)
    if tunnel_meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tunnel")

    status_result = await helper.tunnel_status(name)
    live_by_key = {}
    for t in status_result.get("tunnels", []):
        for p in t.get("peers", []):
            live_by_key[p["public_key"]] = p

    result = []
    for peer in tunnel_meta["peers"]:
        live = live_by_key.get(peer["public_key"], {})
        result.append(
            {
                "id": peer["id"],
                "description": peer["description"],
                "tunnel_address": peer["tunnel_address"],
                "allowed_ips": peer["allowed_ips"],
                "latest_handshake": live.get("latest_handshake"),
                "rx_bytes": live.get("rx_bytes"),
                "tx_bytes": live.get("tx_bytes"),
            }
        )
    return result


@router.post("/tunnels/{name}/peers")
async def create_peer(name: str, payload: PeerCreateRequest, user=Depends(current_user)):
    metadata = _load_metadata()
    tunnel_meta = metadata["tunnels"].get(name)
    if tunnel_meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tunnel")

    status_result = await helper.tunnel_status(name)
    live_tunnels = status_result.get("tunnels", [])
    server_public_key = live_tunnels[0].get("public_key") if live_tunnels else None
    if not server_public_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tunnel is not active — start it before adding peers",
        )

    peer_address = _allocate_peer_address(tunnel_meta)
    requested_allowed_ips = [str(net) for net in payload.allowed_ips]

    keypair = await helper.genkey()
    psk = None
    if payload.preshared_key:
        # wg has no separate "genpsk" subcommand exposed via genkey/pubkey;
        # genkey's output is already 32 cryptographically random bytes,
        # which is exactly what a preshared key is (no curve25519 clamping
        # requirement applies to it), so reusing it here is correct, not a
        # shortcut.
        psk = (await helper.genkey())["private_key"]

    # Server-side AllowedIPs for this peer is its own tunnel address ONLY —
    # seeing this in that context could look wrong next to the client's
    # requested subnets below, but it isn't: see module docstring.
    await helper.add_peer(
        tunnel=name,
        public_key=keypair["public_key"],
        allowed_ips=[peer_address],
        preshared_key=psk,
        keepalive=payload.keepalive,
    )

    peer_id = str(uuid.uuid4())
    tunnel_meta["peers"].append(
        {
            "id": peer_id,
            "description": payload.description,
            "tunnel_address": peer_address,
            "allowed_ips": requested_allowed_ips,
            "public_key": keypair["public_key"],
            "created_by": user.get("sub"),
        }
    )
    _save_metadata(metadata)
    logger.info(
        "peer_created",
        extra={
            "event": "wireguard.peer.created",
            "user": user.get("sub"),
            "tunnel": name,
            "peer_id": peer_id,
            "description": payload.description,
        },
    )

    conf_lines = [
        "[Interface]",
        f"PrivateKey = {keypair['private_key']}",
        f"Address = {peer_address}",
        "",
        "[Peer]",
        f"PublicKey = {server_public_key}",
    ]
    if psk:
        conf_lines.append(f"PresharedKey = {psk}")
    conf_lines.append(f"AllowedIPs = {', '.join(requested_allowed_ips)}")
    if tunnel_meta.get("endpoint"):
        conf_lines.append(f"Endpoint = {tunnel_meta['endpoint']}:{tunnel_meta['listen_port']}")
    else:
        conf_lines.append(f"# Endpoint = <this server's reachable address>:{tunnel_meta['listen_port']}")
    if payload.keepalive:
        conf_lines.append(f"PersistentKeepalive = {payload.keepalive}")
    conf_text = "\n".join(conf_lines) + "\n"

    safe_filename = "".join(c if c.isalnum() or c in "._-" else "_" for c in payload.description) or "peer"
    headers = {"Content-Disposition": f'attachment; filename="{safe_filename}.conf"'}
    return Response(content=conf_text, media_type="text/plain", headers=headers)


@router.delete("/tunnels/{name}/peers/{peer_id}")
async def delete_peer(name: str, peer_id: str, user=Depends(current_user)):
    metadata = _load_metadata()
    tunnel_meta = metadata["tunnels"].get(name)
    if tunnel_meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tunnel")

    peer = next((p for p in tunnel_meta["peers"] if p["id"] == peer_id), None)
    if peer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown peer")

    await helper.remove_peer(tunnel=name, public_key=peer["public_key"])
    tunnel_meta["peers"] = [p for p in tunnel_meta["peers"] if p["id"] != peer_id]
    _save_metadata(metadata)
    logger.info(
        "peer_deleted",
        extra={"event": "wireguard.peer.deleted", "user": user.get("sub"), "tunnel": name, "peer_id": peer_id},
    )
    return {"status": "ok"}


@router.get("/status")
async def status_snapshot():
    return await helper.tunnel_status()
