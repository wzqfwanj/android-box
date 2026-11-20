#!/usr/bin/env python3
"""android-box · 在线真安卓（ARM 云手机）

浏览器里直接操作阿里云无影云手机（Android 12 / arm64）：
- 后端只做两件事：提供静态页面 + 用本机阿里云 AK 换取连接 Ticket（/api/cp/ticket）；
- 画面与触摸走阿里云官方 Web SDK（浏览器直连阿里云串流网关），服务器不转发视频。

仅标准库。默认只监听 127.0.0.1，由网关 Caddy 反向代理到站点子路径 /android/。
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8150"))
CLOUD_PHONE_INSTANCE = os.environ.get("CLOUD_PHONE_INSTANCE", "")
CP_REGION = os.environ.get("CP_REGION", "cn-shanghai")
CP_GROUP_NAME = os.environ.get("CP_GROUP_NAME", "")
ALIYUN_CONFIG = os.environ.get("ALIYUN_CONFIG", os.path.expanduser("~/.aliyun/config.json"))
TICKET_HELPER = os.path.join(BASE_DIR, "tools", "cp_ticket.py")
CLOUDPHONE_HELPER = os.path.join(BASE_DIR, "tools", "cloudphone.py")
CLOUD_PHONE_APK = os.environ.get("CLOUD_PHONE_APK", "/var/lib/android-box/meituan.apk")

POWER_LOCK = threading.Lock()
POWER = {"state": "unknown", "message": "", "instanceId": "", "log": [], "started": 0.0}
_POWER_PROC = {"proc": None}
START_TIME = time.time()
MAX_CP_ASSET = 8 * 1024 * 1024

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/cloud.js": ("cloud.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

CP_MIME = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
}


def read_file(path, limit=MAX_CP_ASSET):
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "android-box/2.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础响应 ----

    def _send(self, status, body, content_type, head_only=False, cache="no-cache"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _json(self, status, payload, head_only=False):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", head_only, cache="no-store")

    def _error(self, status, message, head_only=False):
        self._json(status, {"ok": False, "error": message}, head_only)

    def _prefix(self):
        """网关经 Caddy 剥离 /android 前缀，用 X-Forwarded-Prefix 还原。"""
        return (self.headers.get("X-Forwarded-Prefix") or "").rstrip("/")

    # ---- 路由 ----

    def do_GET(self):
        self._route(head_only=False)

    def do_HEAD(self):
        self._route(head_only=True)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/cp/power":
            return self._power_post()
        if path == "/api/cp/launch":
            return self._launch_post()
        self._error(405, "不支持的请求方法")

    def do_PUT(self):
        self._error(405, "不支持的请求方法")

    def do_DELETE(self):
        self._error(405, "不支持的请求方法")

    def _route(self, head_only):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/health":
            return self._health(head_only)
        if path == "/api/cp/power":
            return self._power_get(head_only)
        if path == "/api/cp/ticket":
            return self._ticket(head_only)
        if path in ("/cloud", "/cloud/"):
            return self._redirect(self._prefix() + "/", head_only)
        if path.startswith("/cp/"):
            return self._cp_asset(path[len("/cp/"):], head_only)
        if path.startswith("/sdk/"):
            return self._cp_asset("sdk/" + path[len("/sdk/"):], head_only)

        static = STATIC_FILES.get(path)
        if static is None:
            return self._error(404, "页面不存在", head_only)
        filename, content_type = static
        body = read_file(os.path.join(BASE_DIR, filename))
        if body is None:
            return self._error(500, "静态文件缺失：%s" % filename, head_only)
        self._send(200, body, content_type, head_only)

    def _redirect(self, location, head_only):
        body = ("跳转中：%s" % location).encode("utf-8")
        self.send_response(308)
        self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


    # ---- 电源：开机 = 创建（含装 APK），关机 = 释放 ----

    def _read_json(self, limit=65536):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return {}

    def _cloudphone_status(self, require_instance=False):
        env = dict(os.environ, ALIYUN_CONFIG=ALIYUN_CONFIG, CP_REGION=CP_REGION)
        if CP_GROUP_NAME:
            env["CP_GROUP_NAME"] = CP_GROUP_NAME
        try:
            proc = subprocess.run([sys.executable, CLOUDPHONE_HELPER, "status", "--json"],
                                  capture_output=True, timeout=90, env=env)
            return json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def _power_get(self, head_only):
        with POWER_LOCK:
            payload = {
                "ok": True,
                "state": POWER["state"],
                "message": POWER["message"],
                "instanceId": POWER["instanceId"],
                "log": POWER["log"][-15:],
            }
        if payload["state"] in ("unknown",):
            info = self._cloudphone_status()
            if info.get("instanceId"):
                payload["state"] = "on"
                payload["instanceId"] = info["instanceId"]
            elif info:
                payload["state"] = "off"
        self._json(200, payload, head_only)

    def _power_post(self):
        body = self._read_json()
        action = str(body.get("action") or "").strip()
        if action not in ("on", "off"):
            return self._error(400, "action 只能是 on / off")
        with POWER_LOCK:
            proc = _POWER_PROC["proc"]
            if proc is not None and proc.poll() is None:
                return self._error(409, "电源操作进行中，请稍候")
            initial = "creating" if action == "on" else "releasing"
            POWER.update(state=initial, message="正在创建云手机…" if action == "on" else "正在释放云手机…",
                         instanceId="", log=[], started=time.time())
        threading.Thread(target=self._power_worker, args=(action,), daemon=True).start()
        self._json(202, {"ok": True, "state": initial})

    def _launch_post(self):
        body = self._read_json()
        package = str(body.get("package") or "com.sankuai.meituan").strip()
        if not package or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._" for ch in package):
            return self._error(400, "package 不合法")
        env = dict(os.environ, ALIYUN_CONFIG=ALIYUN_CONFIG, CP_REGION=CP_REGION)
        if CP_GROUP_NAME:
            env["CP_GROUP_NAME"] = CP_GROUP_NAME
        try:
            proc = subprocess.run([sys.executable, CLOUDPHONE_HELPER, "launch", "--package", package],
                                  capture_output=True, timeout=90, env=env)
            output = (proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")).strip()
        except Exception as exc:  # noqa: BLE001
            return self._error(500, "打开应用失败：%s" % exc)
        if proc.returncode != 0:
            return self._error(503, output[-500:] or "打开应用失败")
        self._json(200, {"ok": True, "output": output[-800:]})

    def _power_worker(self, action):
        env = dict(os.environ, ALIYUN_CONFIG=ALIYUN_CONFIG, CP_REGION=CP_REGION)
        if CP_GROUP_NAME:
            env["CP_GROUP_NAME"] = CP_GROUP_NAME
        cmd = [sys.executable, CLOUDPHONE_HELPER, "release"] if action == "off" else [sys.executable, CLOUDPHONE_HELPER, "create"]
        if action == "on" and os.path.isfile(CLOUD_PHONE_APK):
            cmd += ["--install-apk", CLOUD_PHONE_APK]
        state = "error"
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=env, bufsize=1)
            _POWER_PROC["proc"] = proc
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                with POWER_LOCK:
                    POWER["log"] = (POWER["log"] + [line])[-40:]
                    POWER["message"] = line
            rc = proc.wait()
            info = self._cloudphone_status()
            with POWER_LOCK:
                if rc == 0:
                    state = "on" if action == "on" else "off"
                    POWER["instanceId"] = info.get("instanceId", "") if action == "on" else ""
                    POWER["message"] = "云手机已开机" if action == "on" else "云手机已释放（0 计费）"
                else:
                    state = "error"
                    POWER["message"] = "操作失败（返回码 %s），见日志" % rc
                POWER["state"] = state
        except Exception as exc:  # noqa: BLE001
            with POWER_LOCK:
                POWER["state"] = "error"
                POWER["message"] = "操作异常：%s" % exc
        finally:
            _POWER_PROC["proc"] = None

    # ---- 接口 ----

    def _health(self, head_only):
        self._json(200, {
            "ok": True,
            "service": "android-box",
            "mode": "cloud-phone",
            "cloud_phone_configured": bool(CLOUD_PHONE_INSTANCE or CP_GROUP_NAME),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "uptime": int(time.time() - START_TIME),
        }, head_only)

    def _ticket(self, head_only):
        env = dict(os.environ, CP_REGION=CP_REGION, ALIYUN_CONFIG=ALIYUN_CONFIG)
        if CP_GROUP_NAME:
            env["CP_GROUP_NAME"] = CP_GROUP_NAME
        cmd = [sys.executable, TICKET_HELPER]
        if CLOUD_PHONE_INSTANCE:
            cmd.append(CLOUD_PHONE_INSTANCE)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=40, env=env)
            data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
        except Exception as exc:  # noqa: BLE001
            return self._error(500, "获取 Ticket 失败：%s" % exc, head_only)
        if not data.get("ticket"):
            message = data.get("error") or "没有返回 Ticket"
            if any(token in message for token in ("未创建", "NotFound", "InvalidResourceId", "不存在")):
                message = "云手机当前未创建（已释放）。需要用的时候运行：python tools/cloudphone.py create"
                return self._error(503, message, head_only)
            return self._error(502, message, head_only)
        data["ok"] = True
        self._json(200, data, head_only)

    # ---- 静态资源（无影 Web SDK 全部放 cp/，不随仓库分发）----

    def _cp_asset(self, rel, head_only):
        rel = urllib.parse.unquote(rel).replace("\\", "/")
        full = os.path.normpath(os.path.join(BASE_DIR, "cp", rel))
        root = os.path.normpath(os.path.join(BASE_DIR, "cp"))
        if not (full == root or full.startswith(root + os.sep)):
            return self._error(404, "页面不存在", head_only)
        body = read_file(full)
        if body is None:
            return self._error(404, "页面不存在", head_only)
        ext = os.path.splitext(full)[1].lower()
        self._send(200, body, CP_MIME.get(ext, "application/octet-stream"),
                   head_only, cache="public, max-age=3600")


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("android-box listening on http://%s:%s (cloud-phone=%s)"
          % (HOST, PORT, CLOUD_PHONE_INSTANCE or "未配置"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
