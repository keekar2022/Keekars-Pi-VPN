# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

import asyncio
import logging

import psutil
from fastapi import APIRouter, Depends, HTTPException, status
from pyroute2 import IPRoute, NetlinkError

from app.auth import current_user
from app.models import RouteDeleteRequest, RouteRequest

logger = logging.getLogger("pi_config_ui.routing")

router = APIRouter(prefix="/api/routing", tags=["routing"], dependencies=[Depends(current_user)])

_DEFAULT_TABLE = 254  # "main" routing table


def _existing_interfaces() -> set[str]:
    return set(psutil.net_if_addrs().keys())


def _list_routes_sync(table: int) -> list[dict]:
    with IPRoute() as ipr:
        routes = ipr.get_routes(family=2, table=table)  # AF_INET
        result = []
        for r in routes:
            attrs = dict(r["attrs"])
            oif = attrs.get("RTA_OIF")
            iface = None
            if oif is not None:
                links = ipr.get_links(oif)
                if links:
                    iface = dict(links[0]["attrs"]).get("IFLA_IFNAME")
            result.append(
                {
                    "destination": attrs.get("RTA_DST", "default"),
                    "dst_len": r.get("dst_len"),
                    "gateway": attrs.get("RTA_GATEWAY"),
                    "interface": iface,
                    "table": table,
                }
            )
        return result


def _add_route_sync(destination: str, prefixlen: int, gateway: str | None, iface_idx: int, table: int) -> None:
    with IPRoute() as ipr:
        ipr.route(
            "add",
            dst=destination,
            dst_len=prefixlen,
            gateway=gateway,
            oif=iface_idx,
            table=table,
        )


def _del_route_sync(destination: str, prefixlen: int, iface_idx: int, table: int) -> None:
    with IPRoute() as ipr:
        ipr.route(
            "del",
            dst=destination,
            dst_len=prefixlen,
            oif=iface_idx,
            table=table,
        )


def _iface_index(interface: str) -> int:
    with IPRoute() as ipr:
        idx = ipr.link_lookup(ifname=interface)
        if not idx:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown interface")
        return idx[0]


@router.get("")
async def list_routes(table: int = _DEFAULT_TABLE):
    return await asyncio.to_thread(_list_routes_sync, table)


@router.post("")
async def add_route(payload: RouteRequest, user=Depends(current_user)):
    if payload.interface not in _existing_interfaces():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown interface")
    idx = _iface_index(payload.interface)
    table = payload.table or _DEFAULT_TABLE
    try:
        await asyncio.to_thread(
            _add_route_sync,
            str(payload.destination.network_address),
            payload.destination.prefixlen,
            str(payload.gateway) if payload.gateway else None,
            idx,
            table,
        )
    except NetlinkError as exc:
        logger.error("route_add_failed", extra={"event": "routing.add.failed", "error": str(exc)})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Route could not be added")

    logger.info(
        "route_added",
        extra={"event": "routing.add", "user": user.get("sub"), "destination": str(payload.destination), "interface": payload.interface},
    )
    return {"status": "ok"}


@router.delete("")
async def delete_route(payload: RouteDeleteRequest, user=Depends(current_user)):
    if payload.interface not in _existing_interfaces():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown interface")
    idx = _iface_index(payload.interface)
    table = payload.table or _DEFAULT_TABLE
    try:
        await asyncio.to_thread(
            _del_route_sync,
            str(payload.destination.network_address),
            payload.destination.prefixlen,
            idx,
            table,
        )
    except NetlinkError as exc:
        logger.error("route_delete_failed", extra={"event": "routing.delete.failed", "error": str(exc)})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Route could not be removed")

    logger.info(
        "route_deleted",
        extra={"event": "routing.delete", "user": user.get("sub"), "destination": str(payload.destination), "interface": payload.interface},
    )
    return {"status": "ok"}
