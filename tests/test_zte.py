import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from monitor import config, zte


class ZteTests(unittest.TestCase):
    def test_authenticated_response_parses_real_zte_fields(self):
        response = {"clientsData": {"1": {
            "MacAddr": "aa:bb:cc:dd:ee:ff", "IpAddr": "192.168.5.2",
            "HostName": "test", "Rssi": "-50", "TxRateBps": "100",
        }, "MGET_INST_NUM": 1}}
        with patch.object(zte, "_cookies", return_value={"SID": "test"}), patch.object(
                zte, "_request", return_value=response):
            clients = zte.fetch_clients()
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["name"], "test")
        self.assertEqual(clients[0]["tx_rate_bps"], "100.0")

    def test_expired_session_logs_in_once_and_retries(self):
        response = {"clientsData": {"MGET_INST_NUM": 0}}
        with patch.object(zte, "_cookies", return_value={}), patch.object(
                zte, "_request", side_effect=[PermissionError("login page"), response]), patch.object(
                zte, "_login") as login:
            self.assertEqual(zte.fetch_clients(), [])
        login.assert_called_once()

    def test_cookie_is_not_reused_for_a_different_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps({"base_url": "http://one", "cookies": {"SID": "test"}}))
            with patch.object(zte, "_session_path", return_value=path):
                self.assertEqual(zte._cookies("http://two"), {})

    def test_associated_client_without_ip_is_not_dropped(self):
        with patch.object(zte, "_cookies", return_value={}), patch.object(zte, "_request", return_value={
            "clientsData": {"1": {"MacAddr": "AA:BB:CC:DD:EE:FF", "Rssi": "-50"}, "MGET_INST_NUM": 1}
        }):
            self.assertEqual(zte.fetch_clients()[0]["ip"], "")

    def test_unknown_nonempty_format_is_not_reported_as_empty_network(self):
        with patch.object(zte, "_cookies", return_value={}), patch.object(zte, "_request", return_value={
            "clientsData": {"MGET_INST_NUM": 1, "1": {"unknown": "field"}}
        }), self.assertRaisesRegex(RuntimeError, "字段无法解析"):
            zte.fetch_clients()


if __name__ == "__main__":
    unittest.main()
