# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends

from app.auth import current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["monitor"], dependencies=[Depends(current_user)])

# Prime psutil's internal CPU sample window; the first real call needs a
# baseline to diff against.
psutil.cpu_percent(interval=None)

_last_counters = psutil.net_io_counters(pernic=True)
_last_time = time.monotonic()

# --- Downtime tracking -----------------------------------------------------
#
# This Pi Zero W has no hardware RTC and no fake-hwclock: the system clock
# is wrong (some arbitrary past value) from boot until systemd-timesyncd
# corrects it. psutil.boot_time() (derived from /proc/stat's btime) is NOT
# stable before that correction either — the kernel recomputes it whenever
# the wall clock steps, so it can visibly change mid-boot. Everything below
# only reads/persists/compares boot_time() after confirming NTP sync, or a
# same-boot service restart could be misdetected as a device reboot.

_NTP_SYNC_MARKER = Path("/run/systemd/timesync/synchronized")
_STATE_PATH = Path("/etc/pi-config-ui/monitor/state.json")
_HEARTBEAT_INTERVAL_S = 60

# Written by deploy/maintenance.sh's cmd_boot_check (running as root, via
# pi-config-ui-boot-check.service) when a post-boot health check fails —
# see docs/RUNBOOK.md for what this does/doesn't cover. This app only ever
# reads it and lets the user dismiss it; it never writes it itself.
_ROLLBACK_ALERT_PATH = Path("/etc/pi-config-ui/monitor/rollback_alert.json")

_last_downtime_seconds: float | None = None
_boot_comparison_done = False


def _ntp_synced() -> bool:
    return _NTP_SYNC_MARKER.exists()


def _load_state() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("monitor_state_read_failed", extra={"event": "monitor.state.read_failed", "error": str(exc)})
        return {}


def _save_state(data: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(_STATE_PATH)


async def _heartbeat_tick() -> None:
    global _last_downtime_seconds, _boot_comparison_done

    if not _ntp_synced():
        # Clock isn't trustworthy yet — touch nothing, retry next tick.
        return

    state = _load_state()
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
    _save_state(state)


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


def _uptime_seconds() -> float | None:
    # /proc/uptime's first field is kernel-monotonic seconds since boot —
    # deliberately not psutil.boot_time(), which is wall-clock-derived and
    # was already found unreliable pre-NTP-sync on this hardware (see the
    # downtime-tracking heartbeat above and docs/PROJECT_NOTES.md). Uptime
    # doesn't need any of that NTP-gating machinery since this value is
    # immune to that class of bug entirely.
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


@router.get("/stats")
async def stats():
    global _last_counters, _last_time

    now = time.monotonic()
    elapsed = max(now - _last_time, 1e-6)
    counters = psutil.net_io_counters(pernic=True)

    interfaces = {}
    for name, c in counters.items():
        prev = _last_counters.get(name)
        sent_rate = (c.bytes_sent - prev.bytes_sent) / elapsed if prev else 0
        recv_rate = (c.bytes_recv - prev.bytes_recv) / elapsed if prev else 0
        interfaces[name] = {
            "bytes_sent_per_sec": max(sent_rate, 0),
            "bytes_recv_per_sec": max(recv_rate, 0),
        }

    _last_counters = counters
    _last_time = now

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    top_cpu, top_mem = _top_processes()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "mem_percent": mem.percent,
        "disk_percent": disk.percent,
        "interfaces": interfaces,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "last_downtime_seconds": _last_downtime_seconds,
        "uptime_seconds": _uptime_seconds(),
        "rollback_alert": _load_rollback_alert(),
    }


@router.post("/rollback-alert/dismiss")
async def dismiss_rollback_alert(user=Depends(current_user)):
    _ROLLBACK_ALERT_PATH.unlink(missing_ok=True)
    logger.info("rollback_alert_dismissed", extra={"event": "monitor.rollback_alert.dismissed", "user": user.get("sub")})
    return {"status": "ok"}
