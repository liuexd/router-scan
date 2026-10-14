import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs
from monitor import bootstrap, config, zte

ROOT = Path(__file__).resolve().parent.parent


class ZteRouter(BaseHTTPRequestHandler):
    logins = 0

    def log_message(self, *args):
        pass

    def respond(self, data, cookie=None):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if "login_token_json" in self.path:
            self.respond({"logintoken": "challenge", "_sessionToken": "csrf"}, "SID=anonymous; Path=/")
        elif "initial_info_json" in self.path:
            self.respond({"data": {"model": {"user": "admin"}}})
        elif "GetALLClients" in self.path and "SID=authenticated" in self.headers.get("Cookie", ""):
            self.respond({"clientsData": {"1": {"MacAddr": "AA:BB:CC:DD:EE:FF", "IpAddr": "192.168.1.2", "HostName": "test", "Rssi": "-50"}, "MGET_INST_NUM": 1}})
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>login</html>")

    def do_POST(self):
        data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode(), keep_blank_values=True)
        expected = hashlib.sha256(b"example-passwordchallenge").hexdigest()
        if data.get("Username") == ["admin"] and data.get("Password") == [expected] and data.get("_sessionTOKEN") == ["csrf"]:
            type(self).logins += 1
            self.respond({"login_need_refresh": True, "loginErrType": ""}, "SID=authenticated; Path=/")
        else:
            self.respond({"loginErrType": "e_invalid_user_pwd"})


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        zte._next_login = 0
        zte._login_failures = 0

    def test_real_http_auth_auto_username_and_cookie_reuse(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ZteRouter)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        ZteRouter.logins = 0
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = f"http://127.0.0.1:{server.server_port}"
                credentials = Path(directory) / "credentials.json"
                credentials.write_text(json.dumps({"base_url": base, "username": "", "password": "example-password"}))
                with patch.object(config, "ROUTER_BASE_URL", base), patch.object(
                        config, "URL", base + "/?Action=GetALLClients"), patch.object(
                        config, "CREDENTIALS_FILE", credentials), patch.object(
                        config, "STATE_FILE", Path(directory) / "state.json"):
                    self.assertEqual(zte.fetch_clients()[0]["name"], "test")
                    self.assertEqual(zte.fetch_clients()[0]["ip"], "192.168.1.2")
                    self.assertEqual(ZteRouter.logins, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_failed_login_is_rate_limited(self):
        with patch.object(config, "CREDENTIALS_FILE", Path("does-not-exist.json")):
            with self.assertRaisesRegex(PermissionError, "缺少管理凭据"):
                zte._login("http://example", {})
            with self.assertRaisesRegex(PermissionError, "退避期"):
                zte._login("http://example", {})

    def test_setup_does_not_require_editing_source(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.input", side_effect=[
                "http://192.168.1.1:8090", "", "127.0.0.1", "runtime", ""]), patch(
                "getpass.getpass", return_value="example-password"):
            bootstrap.setup(directory, "zte")
            credentials = json.loads((Path(directory) / "zte_credentials.json").read_text())
            self.assertEqual(credentials["username"], "")
            self.assertEqual(credentials["base_url"], "http://192.168.1.1:8090")
            self.assertEqual(json.loads((Path(directory) / "deployment.json").read_text())["backend"], "zte")

    def test_base_url_rejects_fragment_credentials_and_bad_port(self):
        for value in ["http://host/#/", "http://user:secret@host", "http://host:bad", "ftp://host"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                config._normalize_base_url(value)

    def test_cli_runs_from_another_directory_and_reports_tcp_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            # Keep a socket bound without listening: connection is deterministically refused.
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
                (Path(directory) / "monitor_settings.json").write_text(json.dumps({"router_base_url": f"http://127.0.0.1:{port}"}))
                result = subprocess.run([sys.executable, str(ROOT / "main.py"), "--config-dir", directory, "--backend", "zte", "--check"], cwd=directory, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["stage"], "tcp")
            self.assertTrue((Path(directory) / "runtime").is_dir())

    def test_history_has_a_hard_memory_limit(self):
        from monitor.history import prune_history
        from monitor.utils import ts
        entries = [{"timestamp": ts(), "id": i} for i in range(10)]
        with patch.object(config, "HISTORY_MAX_ENTRIES", 3):
            prune_history(entries)
        self.assertEqual([e["id"] for e in entries], [7, 8, 9])


if __name__ == "__main__":
    unittest.main()
