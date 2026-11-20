#!/usr/bin/env python3
"""android-box 单元测试（标准库 unittest，不依赖真实云手机/阿里云）。"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

os.environ.pop("CLOUD_PHONE_INSTANCE", None)
os.environ["ALIYUN_CONFIG"] = os.path.join(BASE, "tests", "no-such-aliyun-config.json")

import app  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%s" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def fetch(self, path, method="GET", follow=True):
        req = urllib.request.Request(self.base + path, method=method)
        opener = urllib.request.build_opener(NoRedirect) if not follow else urllib.request.build_opener()
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def fetch_with_headers(self, path, headers):
        req = urllib.request.Request(self.base + path, headers=headers)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_health(self):
        status, headers, body = self.fetch("/api/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "cloud-phone")
        self.assertFalse(data["cloud_phone_configured"])
        self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_ticket_without_credentials(self):
        status, _, body = self.fetch("/api/cp/ticket")
        self.assertEqual(status, 502)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("凭据", data["error"])

    def test_ticket_released_is_friendly_503(self):
        class FakeProc:
            stdout = json.dumps({"error": "InvalidResourceId.NotFound"}).encode()

        original = app.subprocess.run
        app.subprocess.run = lambda *a, **kw: FakeProc()
        try:
            status, _, body = self.fetch("/api/cp/ticket")
        finally:
            app.subprocess.run = original
        self.assertEqual(status, 503)
        self.assertIn("cloudphone.py create", json.loads(body)["error"])

    def test_ticket_configured_but_no_credentials(self):
        app.CLOUD_PHONE_INSTANCE = "acp-test"
        try:
            status, _, body = self.fetch("/api/cp/ticket")
        finally:
            app.CLOUD_PHONE_INSTANCE = ""
        self.assertEqual(status, 502)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("凭据", data["error"])

    def test_index_page(self):
        status, headers, body = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"WuyingWebSDK.js", body)

    def test_static_assets(self):
        for path in ("/style.css", "/cloud.js", "/favicon.svg"):
            status, _, body = self.fetch(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body, path)

    def test_cloud_redirect(self):
        status, headers, _ = self.fetch("/cloud/", follow=False)
        self.assertEqual(status, 308)
        self.assertEqual(headers["Location"], "/")

    def test_cloud_redirect_with_prefix(self):
        status, headers, _ = self.fetch_with_headers("/cloud/", {"X-Forwarded-Prefix": "/android"})
        self.assertEqual(status, 308)
        self.assertEqual(headers["Location"], "/android/")

    def test_missing_page(self):
        status, _, body = self.fetch("/nope")
        self.assertEqual(status, 404)
        self.assertFalse(json.loads(body)["ok"])

    def test_cp_path_traversal(self):
        for path in ("/cp/../app.py", "/cp/%2e%2e/app.py", "/sdk/../../app.py"):
            status, _, _ = self.fetch(path)
            self.assertEqual(status, 404, path)

    def test_cp_asset_404(self):
        status, _, _ = self.fetch("/cp/no-such-file.js")
        self.assertEqual(status, 404)

    def test_method_not_allowed(self):
        status, _, _ = self.fetch("/api/health", method="POST")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
