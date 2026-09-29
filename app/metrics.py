# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# Device metric collectors shared by the web dashboard (app/monitor.py) and
# the MQTT telemetry agent (app/telemetry.py), so the two can never report
# different numbers for the same thing.
#
# Deliberately imports nothing from app.config/app.auth: app.config.Settings
# is instantiated at import time and hard-fails without the SSO variables,
# which the telemetry agent has no business needing.

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

# See app/monitor.py's downtime-tracking notes: this hardware has no RTC, so
# the wall clock (and psutil.boot_time(), which is derived from it) is wrong
# from boot until systemd-timesyncd corrects it.
NTP_SYNC_MARKER = Path("/run/systemd/timesync/synchronized")
STATE_PATH = Path("/etc/pi-config-ui/monitor/state.json")

_THERMAL_ROOT = Path("/sys/class/thermal")


def ntp_synced() -> bool:
    return NTP_SYNC_MARKER.exists()


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("monitor_state_read_failed", extra={"event": "monitor.state.read_failed", "error": str(exc)})
        return {}


def save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(STATE_PATH)


def cpu_temp_c() -> float | None:
    # Zone ordering is board-specific: on the Raspberry Pi Zero W the CPU is
    # thermal_zone0, on the Walnut Pi (Allwinner) zone0 is the GPU and the
    # CPU is zone2 — so match on `type`, never on the zone number.
    zones = sorted(_THERMAL_ROOT.glob("thermal_zone*"))
    preferred = [z for z in zones if "cpu" in _zone_type(z)]
    for zone in preferred + zones:
        try:
            return int((zone / "temp").read_text().strip()) / 1000
        except (OSError, ValueError):
            continue
    return None


def _zone_type(zone: Path) -> str:
    try:
        return (zone / "type").read_text().strip().lower()
    except OSError:
        return ""


def uptime_seconds() -> float | None:
    # /proc/uptime's first field is kernel-monotonic seconds since boot —
    # deliberately not psutil.boot_time(), which is wall-clock-derived and
    # unreliable before NTP sync on this hardware.
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def boot_time_utc() -> datetime | None:
    """Wall-clock boot time, or None until the clock is trustworthy."""
    if not ntp_synced():
        return None
    return datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)


class NetRates:
    """Per-interface byte counters plus a rate derived from the last sample."""

    def __init__(self) -> None:
        self._counters = psutil.net_io_counters(pernic=True)
        self._time = time.monotonic()

    def sample(self) -> dict[str, dict[str, float]]:
        now = time.monotonic()
        elapsed = max(now - self._time, 1e-6)
        counters = psutil.net_io_counters(pernic=True)

        result = {}
        for name, c in counters.items():
            prev = self._counters.get(name)
            # A counter going backwards means the interface was recreated;
            # report 0 rather than a negative spike.
            sent_rate = (c.bytes_sent - prev.bytes_sent) / elapsed if prev else 0
            recv_rate = (c.bytes_recv - prev.bytes_recv) / elapsed if prev else 0
            result[name] = {
                "bytes_sent": c.bytes_sent,
                "bytes_recv": c.bytes_recv,
                "bytes_sent_per_sec": max(sent_rate, 0),
                "bytes_recv_per_sec": max(recv_rate, 0),
            }

        self._counters = counters
        self._time = now
        return result
