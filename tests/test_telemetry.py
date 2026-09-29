# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com
#
# app/telemetry.py is imported directly here (not via app.main): it must
# stay free of the SSO settings that app.config demands at import time, so
# the agent can run on a device that has never been given SSO credentials.

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from app import metrics, telemetry


def _settings(**overrides):
    base = {
        "mqtt_host": "192.168.2.201",
        "mqtt_username": "test-device",
        "mqtt_password": "test-password",
        "device_id": "test-pi",
    }
    base.update(overrides)
    return telemetry.TelemetrySettings(**base)


class CpuTempTests(unittest.TestCase):
    def _zones(self, tmp, zones):
        for name, (zone_type, temp) in zones.items():
            zone = tmp / name
            zone.mkdir()
            (zone / "type").write_text(zone_type)
            (zone / "temp").write_text(temp)

    def test_prefers_the_cpu_zone_not_zone0(self):
        # The Walnut Pi's zone0 is the GPU; picking by index reports the
        # wrong die temperature, which is the bug this ordering prevents.
        with mock.patch.object(metrics, "_THERMAL_ROOT", Path(self.tmp)):
            self._zones(
                Path(self.tmp),
                {
                    "thermal_zone0": ("gpu-thermal", "62351"),
                    "thermal_zone2": ("cpu-thermal", "63809"),
                },
            )
            self.assertEqual(metrics.cpu_temp_c(), 63.809)

    def test_returns_none_when_no_thermal_zones_exist(self):
        with mock.patch.object(metrics, "_THERMAL_ROOT", Path(self.tmp)):
            self.assertIsNone(metrics.cpu_temp_c())

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)


class NetRatesTests(unittest.TestCase):
    def test_rate_is_never_negative_after_a_counter_reset(self):
        counters = {"wlan0": mock.Mock(bytes_sent=1000, bytes_recv=2000)}
        with mock.patch("psutil.net_io_counters", return_value=counters):
            rates = metrics.NetRates()
        # Interface recreated: counters restart from zero.
        reset = {"wlan0": mock.Mock(bytes_sent=10, bytes_recv=20)}
        with mock.patch("psutil.net_io_counters", return_value=reset):
            sample = rates.sample()
        self.assertEqual(sample["wlan0"]["bytes_sent_per_sec"], 0)
        self.assertEqual(sample["wlan0"]["bytes_recv_per_sec"], 0)
        self.assertEqual(sample["wlan0"]["bytes_sent"], 10)


class InterfaceSelectionTests(unittest.TestCase):
    available = ["lo", "wlan0", "Syd-Home", "docker0", "br-882c52a8ca63", "hassio"]

    def test_default_drops_loopback_and_container_bridges(self):
        self.assertEqual(
            telemetry._select_interfaces(_settings(), self.available),
            ["wlan0", "Syd-Home"],
        )

    def test_explicit_list_ignores_interfaces_that_are_absent(self):
        settings = _settings(interfaces="wlan0, Bpl-Home ,Syd-Home")
        self.assertEqual(
            telemetry._select_interfaces(settings, self.available),
            ["wlan0", "Syd-Home"],
        )

    def test_slug_makes_interface_names_safe_for_object_ids(self):
        self.assertEqual(telemetry._slug("Syd-Home"), "syd_home")


class DiscoveryPayloadTests(unittest.TestCase):
    def setUp(self):
        self.payload = telemetry._discovery_payload(_settings(), ["wlan0", "Syd-Home"])

    def test_every_component_has_a_unique_id_and_availability_topic(self):
        ids = [c["unique_id"] for c in self.payload["components"].values()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.startswith("test-pi_") for i in ids))
        self.assertTrue(
            all(c["availability_topic"] == self.payload["availability_topic"] for c in self.payload["components"].values())
        )

    def test_byte_counters_are_total_increasing_so_reboots_dont_spike(self):
        self.assertEqual(
            self.payload["components"]["syd_home_bytes_sent"]["state_class"], "total_increasing"
        )

    def test_timestamp_sensor_carries_no_state_class(self):
        self.assertNotIn("state_class", self.payload["components"]["last_boot"])

    def test_is_json_serialisable(self):
        json.loads(json.dumps(self.payload))

    def test_every_component_template_maps_to_a_collected_key(self):
        with mock.patch.object(telemetry, "load_state", return_value={}), mock.patch.object(
            telemetry, "cpu_temp_c", return_value=48.0
        ), mock.patch.object(telemetry, "uptime_seconds", return_value=123.0), mock.patch.object(
            telemetry, "boot_time_utc", return_value=datetime.now(timezone.utc)
        ), mock.patch(
            "psutil.net_io_counters",
            return_value={
                "wlan0": mock.Mock(bytes_sent=1, bytes_recv=2),
                "Syd-Home": mock.Mock(bytes_sent=3, bytes_recv=4),
            },
        ):
            collected = telemetry.collect(["wlan0", "Syd-Home"], metrics.NetRates())
        self.assertEqual(set(self.payload["components"]), set(collected))


class CollectTests(unittest.TestCase):
    def test_unsynced_clock_reports_null_boot_time_rather_than_a_wrong_one(self):
        with mock.patch.object(telemetry, "boot_time_utc", return_value=None), mock.patch.object(
            telemetry, "load_state", return_value={}
        ), mock.patch.object(telemetry, "cpu_temp_c", return_value=None), mock.patch.object(
            telemetry, "uptime_seconds", return_value=60.0
        ), mock.patch("psutil.net_io_counters", return_value={}):
            payload = telemetry.collect([], metrics.NetRates())
        self.assertIsNone(payload["last_boot"])
        self.assertIsNone(payload["cpu_temp_c"])
        self.assertEqual(payload["uptime_seconds"], 60)

    def test_configured_but_missing_interface_publishes_null_not_a_crash(self):
        with mock.patch.object(telemetry, "load_state", return_value={}), mock.patch.object(
            telemetry, "cpu_temp_c", return_value=48.0
        ), mock.patch.object(telemetry, "uptime_seconds", return_value=1.0), mock.patch.object(
            telemetry, "boot_time_utc", return_value=None
        ), mock.patch("psutil.net_io_counters", return_value={}):
            payload = telemetry.collect(["wlan0"], metrics.NetRates())
        self.assertIsNone(payload["wlan0_bytes_sent"])
        self.assertIsNone(payload["wlan0_sent_rate"])


class StateFileTests(unittest.TestCase):
    def test_unreadable_state_file_degrades_to_empty_not_an_exception(self):
        with mock.patch.object(metrics.STATE_PATH.__class__, "exists", return_value=True), mock.patch.object(
            metrics.STATE_PATH.__class__, "read_text", side_effect=OSError("boom")
        ):
            self.assertEqual(metrics.load_state(), {})


if __name__ == "__main__":
    unittest.main()
