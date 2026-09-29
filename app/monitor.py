# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends

from app.auth import current_user
from app.metrics import NetRates, cpu_temp_c, load_state, ntp_synced, save_state, uptime_seconds

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["monitor"], dependencies=[Depends(current_user)])

# Prime psutil's internal CPU sample window; the first real call needs a
# baseline to diff against.
psutil.cpu_percent(interval=None)

_net_rates = NetRates()

# --- Downtime tracking -----------------------------------------------------
#
# This Pi Zero W has no hardware RTC and no fake-hwclock: the system clock
# is wrong (some arbitrary past value) from boot until systemd-timesyncd
# corrects it. psutil.boot_time() (derived from /proc/stat's btime) is NOT
# stable before that correction either — the kernel recomputes it whenever
# the wall clock steps, so it can visibly change mid-boot. Everything below
# only reads/persists/compares boot_time() after confirming NTP sync, or a
# same-boot service restart could be misdetected as a device reboot.

_HEARTBEAT_INTERVAL_S = 60

# Written by deploy/maintenance.sh's cmd_boot_check (running as root, via
# pi-config-ui-boot-check.service) when a post-boot health check fails —
# see docs/RUNBOOK.md for what this does/doesn't cover. This app only ever
# reads it and lets the user dismiss it; it never writes it itself.
_ROLLBACK_ALERT_PATH = Path("/etc/pi-config-ui/monitor/rollback_alert.json")

_last_downtime_seconds: float | None = None
_boot_comparison_done = False


async def _heartbeat_tick() -> None:
    global _last_downtime_seconds, _boot_comparison_done

    if not ntp_synced():
        # Clock isn't trustworthy yet — touch nothing, retry next tick.
        return

    state = load_state()
    now = datetime.now(timezone.utc)
    current_boot_time = psutil.boot_time()

    if not _boot_comparison_done:
        last_boot_time = state.get("last_boot_time")
        last_seen_raw = state.get("last_seen")
        if last_boot_time is None:
            # First-ever run — nothing to compare against.
            _last_downtime_seconds = None
            state["last_downtime_seconds"] = None
        elif current_boot_time != last_boot_time and last_seen_raw:
            # boot_time differs from last run's post-sync reading -> a real
            # reboot happened (not just a Restart=on-failure service bounce).
            # Deliberately measure the outage as (now - last_seen), NOT
            # (boot_time - last_seen): boot_time() is when the KERNEL
            # started, which on this hardware is ~2 minutes before the
            # device is actually usable (NetworkManager alone takes ~57s —
            # see docs/RUNBOOK.md §0/§14). `now`, read here at the first
            # heartbeat tick after this service itself finished starting,
            # is a much closer proxy for "back on the network" than the
            # moment the kernel merely began booting.
            last_seen = datetime.fromisoformat(last_seen_raw)
            _last_downtime_seconds = max((now - last_seen).total_seconds(), 0)
            state["last_downtime_seconds"] = _last_downtime_seconds
        else:
            # Same boot as last recorded run (a service restart, not a
            # device reboot) — restore the previously-computed figure
            # rather than resetting the in-memory value to unknown, since
            # module state doesn't survive a process restart.
            _last_downtime_seconds = state.get("last_downtime_seconds")
        state["last_boot_time"] = current_boot_time
        _boot_comparison_done = True

    state["last_seen"] = now.isoformat()
    save_state(state)


def _load_rollback_alert() -> dict | None:
    if not _ROLLBACK_ALERT_PATH.exists():
        return None
    try:
        return json.loads(_ROLLBACK_ALERT_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("rollback_alert_read_failed", extra={"event": "monitor.rollback_alert.read_failed", "error": str(exc)})
        return None


async def heartbeat_loop() -> None:
    while True:
        try:
            await _heartbeat_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("monitor_heartbeat_tick_failed")
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)

# Kept across requests (not per-request) so each Process's cpu_percent() has
# a prior sample to diff against, same non-blocking pattern as the
# whole-system cpu_percent() above.
_procs: dict[int, psutil.Process] = {}
TOP_N = 10


def _top_processes():
    live_pids = set(psutil.pids())
    for pid in list(_procs):
        if pid not in live_pids:
            del _procs[pid]
    for pid in live_pids:
        if pid not in _procs:
            try:
                proc = psutil.Process(pid)
                proc.cpu_percent(interval=None)  # prime this process's baseline
                _procs[pid] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    rows = []
    for proc in list(_procs.values()):
        try:
            rows.append(
                {
                    "pid": proc.pid,
                    "name": proc.name(),
                    "cpu_percent": proc.cpu_percent(interval=None),
                    "mem_mb": proc.memory_info().rss / (1024 * 1024),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    top_cpu = sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:TOP_N]
    top_mem = sorted(rows, key=lambda r: r["mem_mb"], reverse=True)[:TOP_N]
    return top_cpu, top_mem


@router.get("/stats")
async def stats():
    interfaces = {
        name: {
            "bytes_sent_per_sec": v["bytes_sent_per_sec"],
            "bytes_recv_per_sec": v["bytes_recv_per_sec"],
        }
        for name, v in _net_rates.sample().items()
    }

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    top_cpu, top_mem = _top_processes()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_temp_c": cpu_temp_c(),
        "mem_percent": mem.percent,
        "disk_percent": disk.percent,
        "interfaces": interfaces,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "last_downtime_seconds": _last_downtime_seconds,
        "uptime_seconds": uptime_seconds(),
        "rollback_alert": _load_rollback_alert(),
    }


@router.post("/rollback-alert/dismiss")
async def dismiss_rollback_alert(user=Depends(current_user)):
    _ROLLBACK_ALERT_PATH.unlink(missing_ok=True)
    logger.info("rollback_alert_dismissed", extra={"event": "monitor.rollback_alert.dismissed", "user": user.get("sub")})
    return {"status": "ok"}
