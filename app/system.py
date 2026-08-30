# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# Reboot/shutdown for the System monitor dashboard. pi-config-ui.service
# runs deliberately unprivileged (NoNewPrivileges=true, no root — see
# docs/RUNBOOK.md §7), so this needs a narrow, explicit path to a
# privileged action rather than a blanket grant. `systemctl reboot`/
# `systemctl poweroff`, invoked by a non-root caller, redirect to logind's
# D-Bus API specifically so polkit can gate them per-user — the same
# mechanism (and same subject.user == "pi-config-ui" pattern)
# deploy/polkit-rules/50-pi-config-ui-networkmanager.rules already uses
# for NetworkManager, just a different two action IDs
# (org.freedesktop.login1.reboot / .power-off — see
# deploy/polkit-rules/51-pi-config-ui-power.rules). No new root helper
# daemon needed, unlike WireGuard's pi-wg-helperd.

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import current_user

logger = logging.getLogger("pi_config_ui.system")

router = APIRouter(prefix="/api/system", tags=["system"], dependencies=[Depends(current_user)])


async def _run_systemctl(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "systemctl",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error(
            "systemctl_failed",
            extra={"event": "system.systemctl.failed", "args": args, "stderr": stderr.decode(errors="replace")},
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="System action failed")


@router.post("/reboot")
async def reboot(user=Depends(current_user)):
    logger.info("reboot_requested", extra={"event": "system.reboot", "user": user.get("sub")})
    # Awaited directly, not fire-and-forget: `systemctl reboot` returns as
    # soon as logind has accepted the request (polkit check + inhibitor
    # check), not once the machine has actually finished rebooting — so
    # this still returns quickly, and awaiting it means a polkit/systemctl
    # failure surfaces as a real error instead of a false "ok".
    await _run_systemctl("reboot")
    return {"status": "ok"}


@router.post("/shutdown")
async def shutdown(user=Depends(current_user)):
    logger.info("shutdown_requested", extra={"event": "system.shutdown", "user": user.get("sub")})
    await _run_systemctl("poweroff")
    return {"status": "ok"}
