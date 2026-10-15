import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch
from monitor import config, notifications


class WebhookTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "notifications.json"
        self.path.write_text(json.dumps({"webhook_url": "https://example.test/send?key=example-secret"}))
        for target, value in [("NOTIFICATIONS_FILE", self.path), ("PUSH_NOTIFY_ENABLED", True)]:
            change = patch.object(config, target, value)
            change.start()
            self.addCleanup(change.stop)
        notifications.WEBHOOK_STATUS.update(last_attempt=None, last_success=None, error="")

    def opener(self, response):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
        return opener

    def test_platform_acceptance_and_json_payload(self):
        opener = self.opener({"errcode": 0, "errmsg": "ok"})
        with patch("urllib.request.build_opener", return_value=opener), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(notifications.send_webhook_notification("状态变化"))
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(payload, {"msgtype": "text", "text": {"content": "状态变化"}})
        self.assertTrue(notifications.webhook_status()["last_success"])

    def test_http_200_platform_error_is_a_failure(self):
        with patch("urllib.request.build_opener", return_value=self.opener({"errcode": 93000})), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(notifications.send_webhook_notification("test"))
        self.assertIn("93000", notifications.webhook_status()["error"])
        self.assertIsNone(notifications.webhook_status()["last_success"])

    def test_network_error_does_not_log_webhook_key(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("example-secret")
        log = io.StringIO()
        with patch("urllib.request.build_opener", return_value=opener), contextlib.redirect_stdout(log):
            self.assertFalse(notifications.send_webhook_notification("test"))
        self.assertNotIn("example-secret", log.getvalue())
        self.assertNotIn("example-secret", json.dumps(notifications.webhook_status()))

    def test_disabled_switch_makes_no_request(self):
        with patch.object(config, "PUSH_NOTIFY_ENABLED", False), patch("urllib.request.build_opener") as build:
            self.assertFalse(notifications.send_webhook_notification("test"))
        build.assert_not_called()

    def test_missing_url_is_reported(self):
        self.path.write_text('{}')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(notifications.send_webhook_notification("test"))
        self.assertFalse(notifications.webhook_status()["configured"])
        self.assertIn("未配置", notifications.webhook_status()["error"])
