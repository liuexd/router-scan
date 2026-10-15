import json
import subprocess
import unittest
from unittest.mock import patch

from monitor import config, openwrt
from monitor.activity_probe import ProbeState, evaluate_primary_status


class OpenWrtTests(unittest.TestCase):
    def setUp(self):
        backend = patch.object(config, "ROUTER_BACKEND", "openwrt")
        backend.start()
        self.addCleanup(backend.stop)

    def test_vendor_driver_fallback_reads_only_access_points(self):
        def command(*args):
            if args[1] == "list":
                raise subprocess.CalledProcessError(4, args)
            method = args[3]
            if method == "devices":
                return json.dumps({"devices": ["ra0", "apcli0"]})
            device = json.loads(args[4])["device"]
            if method == "info":
                return json.dumps({"mode": "Master" if device == "ra0" else "Client"})
            return json.dumps({"results": [{"mac": "aa:bb:cc:dd:ee:ff", "signal": -50}]})

        with patch.object(openwrt, "_run", side_effect=command), patch.object(
                openwrt, "_leases", return_value={"AA:BB:CC:DD:EE:FF": ("192.168.5.2", "test")}):
            clients = openwrt.fetch_clients()
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["ip"], "192.168.5.2")
        self.assertEqual(clients[0]["rssi"], "-50")
        self.assertEqual(clients[0]["tx_rate_bps"], "")

    def test_unavailable_traffic_does_not_trigger_false_offline(self):
        state = ProbeState()
        device = {"mac": "AA:BB:CC:DD:EE:FF", "rssi": "-50", "tx_rate_bps": "", "rx_rate_bps": ""}
        for _ in range(config.NO_TRAFFIC_SAMPLE_LIMIT + 3):
            status = evaluate_primary_status(device, state)
            self.assertTrue(status.online)
            self.assertEqual(status.reason, "associated")
        for _ in range(config.DEVICE_ABSENCE_TOLERANCE + 2):
            status = evaluate_primary_status(None, state)
        self.assertFalse(status.online)

    def test_real_bus_error_is_not_hidden_by_fallback(self):
        with patch.object(openwrt, "_run", side_effect=subprocess.CalledProcessError(1, "ubus")):
            with self.assertRaises(subprocess.CalledProcessError):
                openwrt.fetch_clients()


if __name__ == "__main__":
    unittest.main()
