"""Secure private remote mode and identity-bound access regressions."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from pm_app.remote import RemoteAccessConfig, decode_identity_header, normalize_tailnet_host
from pm_app.web import AppState, LocalServer
from remote_client import validate_server_url
from remote_server import config_from_status
from test_app import FakeClient, ROOT


class RemoteConfigTests(unittest.TestCase):
    def test_tailnet_host_and_client_url_are_strict(self):
        self.assertEqual(normalize_tailnet_host("MY-PC.tail-name.ts.net."), "my-pc.tail-name.ts.net")
        self.assertEqual(validate_server_url("https://my-pc.tail-name.ts.net/"), "https://my-pc.tail-name.ts.net")
        for bad in (
            "http://my-pc.tail-name.ts.net",
            "https://example.com",
            "https://my-pc.tail-name.ts.net:8443",
            "https://my-pc.tail-name.ts.net/path",
            "https://user@my-pc.tail-name.ts.net",
        ):
            with self.assertRaises(ValueError):
                validate_server_url(bad)

    def test_identity_header_decoding_and_normalization(self):
        cfg = RemoteAccessConfig("server.example.ts.net", "Owner@Example.COM")
        self.assertEqual(cfg.owner_login, "owner@example.com")
        self.assertEqual(decode_identity_header("OWNER@example.com"), "owner@example.com")

    def test_tailscale_status_selects_server_owner_identity(self):
        cfg = config_from_status({
            "BackendState": "Running",
            "Self": {"DNSName": "My-PC.tail-name.ts.net.", "UserID": 42},
            "User": {"42": {"LoginName": "owner@example.com"},
                     "77": {"LoginName": "someone-else@example.com"}},
        })
        self.assertEqual(cfg.host, "my-pc.tail-name.ts.net")
        self.assertEqual(cfg.owner_login, "owner@example.com")


class RemoteHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = RemoteAccessConfig("server.example.ts.net", "owner@example.com")
        self.state = AppState(ROOT, Path(self.temp.name), FakeClient(), remote_access=self.config)
        self.state.auth.setup_first_admin("admin", "Owner", "very-secret-password")
        self.server = LocalServer(("127.0.0.1", 0), self.state)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, value=None, headers=None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = None if value is None else json.dumps(value).encode("utf-8")
        con.request(method, path, body, headers or {})
        response = con.getresponse()
        status, hdr, data = response.status, dict(response.getheaders()), response.read()
        con.close()
        return status, hdr, data

    def remote_headers(self, *, login="owner@example.com", origin=None, proto="https"):
        headers = {
            "Host": "127.0.0.1:%d" % self.port,
            "X-Forwarded-Host": self.config.host,
            "X-Forwarded-Proto": proto,
            "Tailscale-User-Login": login,
        }
        if origin:
            headers["Origin"] = origin
        return headers

    def test_remote_get_requires_exact_tailscale_identity(self):
        ok = self.remote_headers()
        status, headers, data = self.request("GET", "/api/auth/status", headers=ok)
        self.assertEqual(status, 200)
        self.assertIn("Strict-Transport-Security", headers)
        self.assertTrue(json.loads(data)["remote_access"])

        wrong = self.remote_headers(login="other@example.com")
        self.assertEqual(self.request("GET", "/api/auth/status", headers=wrong)[0], 403)

        missing = dict(ok); missing.pop("Tailscale-User-Login")
        self.assertEqual(self.request("GET", "/api/auth/status", headers=missing)[0], 403)

        bad_proto = self.remote_headers(proto="http")
        self.assertEqual(self.request("GET", "/api/auth/status", headers=bad_proto)[0], 403)

    def test_remote_login_requires_remote_origin_and_sets_secure_cookie(self):
        headers = self.remote_headers(origin=self.config.origin)
        headers["Content-Type"] = "application/json"
        status, response_headers, data = self.request("POST", "/api/auth/login",
            {"username": "admin", "password": "very-secret-password"}, headers)
        self.assertEqual(status, 200)
        cookie = response_headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(json.loads(data)["user"]["username"], "admin")

        bad = self.remote_headers(origin="https://evil.example")
        bad["Content-Type"] = "application/json"
        self.assertEqual(self.request("POST", "/api/auth/login",
            {"username": "admin", "password": "very-secret-password"}, bad)[0], 403)

    def test_local_loopback_access_still_works(self):
        self.assertEqual(self.request("GET", "/api/auth/status")[0], 200)

    def test_login_rate_limit_engages(self):
        key = "tailscale:owner@example.com"
        for _ in range(8):
            self.state.record_login_failure(key)
        self.assertFalse(self.state.login_allowed(key))
        self.state.clear_login_failures(key)
        self.assertTrue(self.state.login_allowed(key))


class RemoteBootstrapTests(unittest.TestCase):
    def test_first_account_cannot_be_created_through_remote_proxy(self):
        temp = tempfile.TemporaryDirectory()
        try:
            cfg = RemoteAccessConfig("server.example.ts.net", "owner@example.com")
            state = AppState(ROOT, Path(temp.name), FakeClient(), remote_access=cfg)
            server = LocalServer(("127.0.0.1", 0), state)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                con = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                body = json.dumps({"username":"admin","display_name":"Owner","password":"very-secret-password"}).encode()
                headers = {
                    "Host": f"127.0.0.1:{server.server_port}",
                    "X-Forwarded-Host": cfg.host,
                    "X-Forwarded-Proto": "https",
                    "Tailscale-User-Login": cfg.owner_login,
                    "Origin": cfg.origin,
                    "Content-Type": "application/json",
                }
                con.request("POST", "/api/auth/setup", body, headers)
                response = con.getresponse(); data = response.read(); con.close()
                self.assertEqual(response.status, 403)
                self.assertFalse(state.auth.has_users())
                self.assertIn("основном компьютере", json.loads(data)["error"])
            finally:
                server.shutdown(); server.server_close(); thread.join()
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
