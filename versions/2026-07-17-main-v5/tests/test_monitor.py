import unittest
from unittest.mock import patch

from monitor import config
from monitor.app import TargetRuntime, _compute_dynamic_interval, _update_target
from monitor.fetcher import (
    RouterAuthenticationError,
    _probe_targets,
    parse_clients_auto,
    scan_once,
)
from monitor.targets import TargetSpec, configured_targets, is_device_online, match_device_config


class TargetTests(unittest.TestCase):
    def test_device_matches_mac_or_case_insensitive_name(self):
        device = {"mac": "aa:bb:cc:dd:ee:ff", "name": "Living Room Phone", "rssi": "-45"}
        self.assertTrue(match_device_config(device, "AA:BB:CC:DD:EE:FF", ""))
        self.assertTrue(match_device_config(device, "", "room phone"))
        self.assertFalse(match_device_config(device, "00:00:00:00:00:00", "tablet"))

    def test_two_targets_are_configured_independently(self):
        with (
            patch.object(config, "TARGET_MAC", "AA"),
            patch.object(config, "TARGET_NAME_CONTAINS", ""),
            patch.object(config, "SECOND_TARGET_MAC", "BB"),
            patch.object(config, "SECOND_TARGET_NAME_CONTAINS", ""),
        ):
            self.assertEqual([target.key for target in configured_targets()], ["primary", "secondary"])

    def test_rssi_threshold_is_applied(self):
        with patch.object(config, "RSSI_THRESHOLD", 66):
            self.assertTrue(is_device_online({"rssi": "-45"}))
            self.assertFalse(is_device_online({"rssi": "-80"}))
            self.assertFalse(is_device_online({"rssi": ""}))


class RuntimeTests(unittest.TestCase):
    def test_target_state_is_not_shared(self):
        one = TargetSpec("primary", "设备一", "AA", "")
        two = TargetSpec("secondary", "设备二", "BB", "")
        devices = [
            {"mac": "AA", "name": "one", "ip": "1.1.1.1", "rssi": "-40"},
            {"mac": "BB", "name": "two", "ip": "1.1.1.2", "rssi": "-90"},
        ]
        with (
            patch("monitor.app.check_target", side_effect=[True, True]),
            patch("monitor.app.record_history"),
            patch("monitor.app.notify_status_change"),
        ):
            one_online, _ = _update_target(devices, one, TargetRuntime(), True)
            two_online, _ = _update_target(devices, two, TargetRuntime(), False)
        self.assertTrue(one_online)
        self.assertFalse(two_online)

    def test_dynamic_interval_stays_in_bounds(self):
        with (
            patch.object(config, "DYNAMIC_INTERVAL_MIN_SECONDS", 1),
            patch.object(config, "DYNAMIC_INTERVAL_MAX_SECONDS", 6),
            patch.object(config, "DYNAMIC_INTERVAL_MAX_DOWNS", 5),
        ):
            events = []
            self.assertEqual(_compute_dynamic_interval(False, False, events, 100), 1)
            self.assertGreaterEqual(_compute_dynamic_interval(True, True, events, 101), 1)
            self.assertLessEqual(_compute_dynamic_interval(True, False, events, 102), 6)

    def test_both_online_devices_are_probed(self):
        devices = [
            {"mac": "AA", "name": "one", "ip": "1.1.1.1", "rssi": "-40"},
            {"mac": "BB", "name": "two", "ip": "1.1.1.2", "rssi": "-40"},
        ]
        with (
            patch.object(config, "TARGET_MAC", "AA"),
            patch.object(config, "TARGET_NAME_CONTAINS", ""),
            patch.object(config, "SECOND_TARGET_MAC", "BB"),
            patch.object(config, "SECOND_TARGET_NAME_CONTAINS", ""),
            patch.object(config, "ARPING_BEFORE_FETCH", True),
            patch.object(config, "ARPING_MODE", "auto"),
            patch("monitor.fetcher._arping_target", return_value=True) as probe,
        ):
            results = _probe_targets(devices)
        self.assertEqual(results, {"primary": True, "secondary": True})
        self.assertEqual(probe.call_count, 2)

    def test_failed_authentication_preserves_last_known_state(self):
        previous = {
            "AA": {"name": "phone", "ip": "1.1.1.1", "rssi": "-45", "online": True}
        }
        with (
            patch("monitor.fetcher.fetch_url", return_value=("<html><script src='/jquery/static/js/app.1.js'></script></html>", "text/html")),
            patch("monitor.fetcher.refresh_cookie_from_router", return_value=""),
        ):
            devices, new_devices, gone_devices, cookie, _ = scan_once(previous, "SID=expired")
        self.assertEqual(devices[0]["mac"], "AA")
        self.assertEqual(new_devices, [])
        self.assertEqual(gone_devices, [])
        self.assertEqual(cookie, "SID=expired")


class ParserTests(unittest.TestCase):
    def test_login_page_is_identified_before_html_table_parsing(self):
        page = "<html><title>Router</title><script src='/jquery/static/js/app.123.js'></script></html>"
        with self.assertRaises(RouterAuthenticationError):
            parse_clients_auto(page, "text/html")

    def test_empty_json_client_response_is_valid(self):
        self.assertEqual(parse_clients_auto('{"clients": []}', "application/json"), [])


class SettingsTests(unittest.TestCase):
    def test_second_target_settings_are_saved(self):
        with patch("monitor.config.save_monitor_settings"):
            settings = config.set_monitor_settings(
                second_target_mac="11:22:33:44:55:66",
                second_target_name_contains="tablet",
            )
        self.assertEqual(settings["second_target_mac"], "11:22:33:44:55:66")
        self.assertEqual(settings["second_target_name_contains"], "tablet")


if __name__ == "__main__":
    unittest.main()
