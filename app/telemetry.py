# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# MQTT telemetry agent: publishes this device's health to a broker using
# Home Assistant's MQTT Discovery, so both Pis appear in HA with no
# YAML-side configuration at all.
#
# Runs as its own systemd unit (deploy/pi-telemetryd.service), NOT inside
# the web app: it must keep reporting across app restarts, and it needs
# none of the app's privileges. Everything it reads (/proc, /sys, psutil)
# is available unprivileged, so this process gets no capabilities and no
# writable paths — it is a pure outbound MQTT client. Nothing here touches
# NetworkManager, routing or WireGuard, which is what makes it safe to
# deploy to a remote device.
#
# Downtime is deliberately NOT computed here. app/monitor.py's heartbeat is
# the sole writer of the shared state file; this agent only ever reads it,
# so the two processes can never race. "Is it down right now" is answered
# by the MQTT Last Will on the availability topic instead.
#
# Run: /opt/pi-config-ui/venv/bin/python -m app.telemetry

import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time

import paho.mqtt.client as mqtt
import psutil
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__
from app.metrics import NetRates, boot_time_utc, cpu_temp_c, load_state, uptime_seconds

logger = logging.getLogger("pi_config_ui.telemetry")

DISCOVERY_PREFIX = "homeassistant"
TOPIC_ROOT = "pi-telemetry"


class TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TELEMETRY_", env_file=None, extra="ignore")

    mqtt_host: str
    mqtt_port: int = 1883
    mqtt_username: str
    mqtt_password: str

    # Defaults to the hostname so a new device works without being told its
    # own name; override only when two devices would otherwise collide.
    device_id: str = socket.gethostname()
    device_name: str = ""

    # Comma-separated. Empty means "every interface except loopback and
    # container/bridge noise" — see _select_interfaces.
    interfaces: str = ""
    interval_s: int = 60


def _slug(value: str) -> str:
    """MQTT object_ids allow far less than an interface name does."""
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


# docker0/br-*/veth* are present on the Walnut Pi (it runs containers) and
# are noise in a device-health dashboard.
_IGNORED_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "hassio")


def _select_interfaces(settings: TelemetrySettings, available: list[str]) -> list[str]:
    if settings.interfaces.strip():
        wanted = [i.strip() for i in settings.interfaces.split(",") if i.strip()]
        # Keep configured-but-currently-absent interfaces out of discovery
        # rather than publishing entities that would sit at "unknown".
        return [i for i in wanted if i in available]
    return [i for i in available if not i.startswith(_IGNORED_IFACE_PREFIXES)]


def _sensor(name: str, key: str, unit: str | None, device_class: str | None, state_class: str | None, icon: str | None = None) -> dict:
    component = {
        "platform": "sensor",
        "name": name,
        "value_template": "{{ value_json.%s }}" % key,
        # Without this HA shows a hard "unknown" whenever a metric is
        # unavailable (no thermal zone, clock not yet NTP-synced) instead of
        # greying the entity out.
        "availability_template": "{{ 'online' if value_json.%s is not none else 'offline' }}" % key,
        "availability_topic": None,  # filled in by _discovery_payload
    }
    if state_class:
        component["state_class"] = state_class
    if unit:
        component["unit_of_measurement"] = unit
    if device_class:
        component["device_class"] = device_class
    if icon:
        component["icon"] = icon
    return component


def _components(interfaces: list[str]) -> dict[str, dict]:
    components = {
        "cpu_temp_c": _sensor("CPU temperature", "cpu_temp_c", "°C", "temperature", "measurement"),
        "cpu_percent": _sensor("CPU usage", "cpu_percent", "%", None, "measurement", "mdi:cpu-64-bit"),
        "memory_percent": _sensor("Memory usage", "memory_percent", "%", None, "measurement", "mdi:memory"),
        "memory_used_mb": _sensor("Memory used", "memory_used_mb", "MB", "data_size", "measurement"),
        "disk_percent": _sensor("Disk usage", "disk_percent", "%", None, "measurement", "mdi:harddisk"),
        "disk_free_gb": _sensor("Disk free", "disk_free_gb", "GB", "data_size", "measurement"),
        "uptime_seconds": _sensor("Uptime", "uptime_seconds", "s", "duration", "measurement"),
        # A timestamp rather than a counter, so HA renders "3 days ago" and
        # the value only changes when the device actually reboots.
        "last_boot": _sensor("Last boot", "last_boot", None, "timestamp", None),
        "last_downtime_seconds": _sensor("Last downtime", "last_downtime_seconds", "s", "duration", None),
    }
    for iface in interfaces:
        key = _slug(iface)
        for direction, label in (("sent", "transmitted"), ("recv", "received")):
            # total_increasing (not total) is what lets HA handle the
            # counter resetting to zero when the device reboots.
            components[f"{key}_bytes_{direction}"] = _sensor(
                f"{iface} {label}", f"{key}_bytes_{direction}", "B", "data_size", "total_increasing"
            )
            components[f"{key}_{direction}_rate"] = _sensor(
                f"{iface} {label} rate", f"{key}_{direction}_rate", "B/s", "data_rate", "measurement"
            )
    return components


def _discovery_payload(settings: TelemetrySettings, interfaces: list[str]) -> dict:
    device_id = settings.device_id
    availability_topic = f"{TOPIC_ROOT}/{device_id}/availability"
    components = _components(interfaces)
    for key, component in components.items():
        component["unique_id"] = f"{device_id}_{key}"
        component["availability_topic"] = availability_topic

    uname = os.uname()
    return {
        "device": {
            "identifiers": [device_id],
            "name": settings.device_name or device_id,
            "manufacturer": "Keekar",
            "model": uname.machine,
            "sw_version": f"pi-config-ui {__version__} ({uname.release})",
        },
        "origin": {"name": "pi-config-ui", "sw_version": __version__},
        "state_topic": f"{TOPIC_ROOT}/{device_id}/state",
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "qos": 1,
        "components": components,
    }


def collect(interfaces: list[str], rates: NetRates) -> dict:
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    boot = boot_time_utc()

    payload = {
        "cpu_temp_c": _round(cpu_temp_c(), 1),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory_percent": mem.percent,
        "memory_used_mb": _round(mem.used / (1024 * 1024), 1),
        "disk_percent": disk.percent,
        "disk_free_gb": _round(disk.free / (1024**3), 2),
        "uptime_seconds": _round(uptime_seconds(), 0),
        "last_boot": boot.isoformat() if boot else None,
        "last_downtime_seconds": load_state().get("last_downtime_seconds"),
    }

    sample = rates.sample()
    for iface in interfaces:
        key = _slug(iface)
        counters = sample.get(iface)
        payload[f"{key}_bytes_sent"] = counters["bytes_sent"] if counters else None
        payload[f"{key}_bytes_recv"] = counters["bytes_recv"] if counters else None
        payload[f"{key}_sent_rate"] = _round(counters["bytes_sent_per_sec"], 1) if counters else None
        payload[f"{key}_recv_rate"] = _round(counters["bytes_recv_per_sec"], 1) if counters else None
    return payload


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _build_client(settings: TelemetrySettings, interfaces: list[str], publish_state) -> mqtt.Client:
    device_id = settings.device_id
    availability_topic = f"{TOPIC_ROOT}/{device_id}/availability"

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"pi-telemetry-{device_id}",
    )
    client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    client.will_set(availability_topic, "offline", qos=1, retain=True)
    # Pi Zero on Wi-Fi: back off rather than hammer a broker that's gone.
    client.reconnect_delay_set(min_delay=5, max_delay=300)

    def on_connect(client_, _userdata, _flags, reason_code, _properties=None):
        if reason_code != 0:
            logger.error(
                "telemetry_connect_refused",
                extra={"event": "telemetry.connect.refused", "reason": str(reason_code)},
            )
            return
        # Republished on every (re)connect, not just the first: the broker
        # may have been reinstalled and lost its retained messages.
        client_.publish(
            f"{DISCOVERY_PREFIX}/device/{device_id}/config",
            json.dumps(_discovery_payload(settings, interfaces)),
            qos=1,
            retain=True,
        )
        client_.publish(availability_topic, "online", qos=1, retain=True)
        # Publish state here rather than waiting for the next tick: the
        # agent's own first loop iteration runs before connect_async has
        # finished, and a QoS 0 publish while disconnected is dropped
        # silently — which left HA with no state at all for a full interval.
        publish_state(client_)
        logger.info(
            "telemetry_connected",
            extra={"event": "telemetry.connected", "device_id": device_id, "interfaces": interfaces},
        )

    def on_disconnect(_client, _userdata, _flags, reason_code, _properties=None):
        logger.warning(
            "telemetry_disconnected",
            extra={"event": "telemetry.disconnected", "reason": str(reason_code)},
        )

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stdout)
    settings = TelemetrySettings()

    rates = NetRates()
    interfaces = _select_interfaces(settings, list(psutil.net_io_counters(pernic=True)))
    psutil.cpu_percent(interval=None)  # prime the sample window

    state_topic = f"{TOPIC_ROOT}/{settings.device_id}/state"

    def publish_state(client_) -> None:
        try:
            client_.publish(state_topic, json.dumps(collect(interfaces, rates)), qos=0, retain=True)
        except Exception:
            logger.exception("telemetry_publish_failed")

    client = _build_client(settings, interfaces, publish_state)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # connect_async + loop_start so a broker that is unreachable at boot
    # (Wi-Fi or the WireGuard tunnel still coming up) retries in the
    # background instead of crash-looping the unit.
    client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=max(settings.interval_s * 2, 60))
    client.loop_start()

    while not stop.is_set():
        # While disconnected, skip rather than publish: QoS 0 would be
        # dropped anyway, and queueing retained state for a broker that has
        # been gone for hours only delivers a burst of stale readings.
        if client.is_connected():
            publish_state(client)
        stop.wait(settings.interval_s)

    # Retained "offline" on a clean stop, so a planned restart looks
    # different in HA from the Last Will of an unplanned drop.
    client.publish(f"{TOPIC_ROOT}/{settings.device_id}/availability", "offline", qos=1, retain=True)
    time.sleep(0.5)
    client.loop_stop()
    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
