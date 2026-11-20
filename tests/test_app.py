#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黑盒测试：不依赖真实 adb/ffmpeg，只验证 HTTP 层与接口校验。

运行：python -m unittest discover -s tests -v
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 让 adb/ffmpeg 指向不存在的命令：接口应优雅降级而不是抛异常
os.environ["ADB_BIN"] = "adb-not-installed"
os.environ["FFMPEG_BIN"] = "ffmpeg-not-installed"

import app  # noqa: E402  (import 顺序是刻意的)


def request(method, url, payload=None, raw=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
        headers["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class AndroidBoxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base = "http://%s:%d" % (host, port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_index_page(self):
        status, headers, body = request("GET", self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        html = body.decode("utf-8")
        self.assertIn("<title>安卓系统 · 在线真机</title>", html)
        self.assertIn('src="app.js"', html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

    def test_frontend_stream_url(self):
        status, _, body = request("GET", self.base + "/app.js")
        self.assertEqual(status, 200)
        js = body.decode("utf-8")
        self.assertIn("stream.mjpg", js)
        self.assertIn("api/input", js)
        self.assertIn("api/install", js)

    def test_static_assets(self):
        for path, ctype in (
            ("/style.css", "text/css"),
            ("/app.js", "text/javascript"),
            ("/favicon.svg", "image/svg+xml"),
        ):
            status, headers, body = request("GET", self.base + path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, headers["Content-Type"], path)
            self.assertTrue(body, path)

    def test_health(self):
        status, _, body = request("GET", self.base + "/api/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "android-box")

    def test_state_without_device(self):
        status, _, body = request("GET", self.base + "/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["adb_online"])
        self.assertEqual(payload["serial"], app.ADB_SERIAL)
        self.assertIn("stream", payload)

    def test_apps_without_device(self):
        status, _, body = request("GET", self.base + "/api/apps")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["packages"], [])

    def test_input_validation(self):
        cases = [
            ({"type": "nope"}, 400),
            ({"type": "key", "key": "NOT_A_KEY"}, 400),
            ({"type": "text", "text": ""}, 400),
            ({"type": "text", "text": "中文"}, 400),  # 仅支持 ASCII
            ({}, 400),
        ]
        for payload, expected in cases:
            status, _, _ = request("POST", self.base + "/api/input", payload)
            self.assertEqual(status, expected, payload)

    def test_input_reports_adb_failure(self):
        status, _, body = request("POST", self.base + "/api/input", {"type": "tap", "x": 10, "y": 20})
        self.assertEqual(status, 502)
        self.assertIn("error", json.loads(body))

    def test_install_rejects_empty(self):
        status, _, _ = request("POST", self.base + "/api/install", raw=b"")
        self.assertEqual(status, 400)

    def test_unknown_routes(self):
        for path in ("/nope", "/api/nope", "/../app.py"):
            status, _, _ = request("GET", self.base + path)
            self.assertEqual(status, 404, path)

    def test_unsupported_method(self):
        status, _, _ = request("PUT", self.base + "/api/state")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main(verbosity=2)
