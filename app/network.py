# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

import asyncio
import logging
import os
import re

import psutil
from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import current_user
from app.models import NetworkConfigRequest, WifiConnectRequest

logger = logging.getLogger("pi_config_ui.network")

router = APIRouter(prefix="/api/network", tags=["network"], dependencies=[Depends(current_user)])

_CONN_PROFILE_PREFIX = "pi-config-ui-"


def _existing_interfaces() -> set[str]:
    return set(psutil.net_if_addrs().keys())


def _parse_nmcli_terse(output: str) -> list[list[str]]:
    """Split nmcli `-t` output into fields, honoring its `\\:`/`\\\\` escaping
    so a colon inside a field (e.g. a BSSID, or a rare SSID) doesn't get
    mistaken for a field separator."""
    rows = []
    for line in output.splitlines():
        if not line:
            continue
        fields = re.split(r"(?<!\\):", line)
        rows.append([f.replace("\\:", ":").replace("\\\\", "\\") for f in fields])
    return rows


async def _run_nmcli(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "nmcli",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error(
            "nmcli_failed",
            extra={"event": "network.nmcli.failed", "args": args, "stderr": stderr.decode(errors="replace")},
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Network operation failed")
    return stdout.decode(errors="replace")


@router.get("/interfaces")
async def list_interfaces():
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    return [
        {
            "name": name,
            "is_up": stats[name].isup if name in stats else False,
            "addresses": [a.address for a in addrs.get(name, [])],
        }
        for name in addrs
    ]


@router.post("/configure")
async def configure_interface(payload: NetworkConfigRequest, user=Depends(current_user)):
    if payload.interface not in _existing_interfaces():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown interface")

    conn_name = f"{_CONN_PROFILE_PREFIX}{payload.interface}"
    if payload.method.value == "dhcp":
        await _run_nmcli("con", "mod", conn_name, "ipv4.method", "auto")
    else:
        addr = str(payload.address)
        await _run_nmcli("con", "mod", conn_name, "ipv4.method", "manual", "ipv4.addresses", addr)
        if payload.gateway:
            await _run_nmcli("con", "mod", conn_name, "ipv4.gateway", str(payload.gateway))
        if payload.dns:
            await _run_nmcli("con", "mod", conn_name, "ipv4.dns", ",".join(str(d) for d in payload.dns))

    await _run_nmcli("con", "up", conn_name)
    logger.info(
        "interface_configured",
        extra={"event": "network.configure", "user": user.get("sub"), "interface": payload.interface, "method": payload.method.value},
    )
    return {"status": "ok"}


@router.get("/wifi/known")
async def list_known_wifi():
    # A saved connection's profile NAME (used to reconnect via `connection
    # up <name>`) is not always its SSID (used to join/update via `device
    # wifi connect <ssid>`) — e.g. a netplan-managed profile here is named
    # "netplan-wlan0-keekar02" for SSID "keekar02". Bulk `connection show`
    # only exposes a fixed column set with no SSID field, so the actual
    # SSID needs a second, per-connection property lookup.
    output = await _run_nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
    results = []
    for name, conn_type in _parse_nmcli_terse(output):
        if conn_type not in ("802-11-wireless", "wifi"):
            continue
        ssid_output = await _run_nmcli("-t", "-f", "802-11-wireless.ssid", "connection", "show", name)
        ssid = ssid_output.strip().split(":", 1)[-1] or name
        results.append({"name": name, "ssid": ssid})
    return results


@router.get("/wifi/scan")
async def scan_wifi():
    output = await _run_nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes")
    best_by_ssid: dict[str, dict] = {}
    for row in _parse_nmcli_terse(output):
        ssid, signal, security = (row + ["", "", ""])[:3]
        if not ssid:
            continue
        signal_int = int(signal) if signal.isdigit() else 0
        existing = best_by_ssid.get(ssid)
        if existing is None or signal_int > existing["signal"]:
            best_by_ssid[ssid] = {"ssid": ssid, "signal": signal_int, "security": security or "open"}
    return sorted(best_by_ssid.values(), key=lambda r: r["signal"], reverse=True)


@router.post("/wifi/connect")
async def wifi_connect(payload: WifiConnectRequest, user=Depends(current_user)):
    if payload.psk:
        # KNOWN LIMITATION: nmcli takes the PSK as a CLI argument, which is
        # briefly visible to other local users via `ps`. Hardening
        # follow-up: switch to NetworkManager's D-Bus AddConnection2
        # (secrets passed as a D-Bus method argument, never on argv/disk).
        # Acceptable for now since this device has no other local users,
        # but do not reuse this pattern on a shared multi-user host.
        await _run_nmcli("device", "wifi", "connect", payload.ssid, "password", payload.psk)
    else:
        # No password given: reconnect an already-known network by its
        # saved profile name without changing it. A profile's name isn't
        # always its SSID (see WifiConnectRequest.connection_name), so
        # fall back to ssid only for profiles nmcli itself would have
        # named after it.
        await _run_nmcli("connection", "up", payload.connection_name or payload.ssid)
    logger.info(
        "wifi_connected",
        extra={"event": "network.wifi_connect", "user": user.get("sub"), "ssid": payload.ssid},
    )
    return {"status": "ok"}
