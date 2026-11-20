#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""android-box · 在线真安卓（容器化 Android）后端

职责（全部基于 Python 标准库）：
  1. 把设备屏幕转成浏览器可看的 MJPEG 流（adb screenrecord → ffmpeg → multipart）
  2. 接收浏览器触摸/按键事件，用 adb input 注入真实 Android 系统
  3. 上传并安装 APK（adb install）
  4. 提供设备状态查询

外部命令依赖（部署机已 apt 安装）：adb、ffmpeg。
不做应用内登录：线上由 Caddy 全站 Basic Auth 统一保护。

环境变量：
  HOST        监听地址，默认 0.0.0.0
  PORT        监听端口，默认 8150
  ADB_SERIAL  adb 设备串号，默认 127.0.0.1:5555（redroid 容器）
  STREAM_W    推流最大宽度，默认 480
  STREAM_FPS  推流帧率，默认 8
  STREAM_Q    MJPEG 质量（2~31，越小越好），默认 7

启动：python app.py  →  http://127.0.0.1:8150
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8150"))
ADB_SERIAL = os.environ.get("ADB_SERIAL", "127.0.0.1:5555")
STREAM_W = int(os.environ.get("STREAM_W", "480"))
STREAM_FPS = int(os.environ.get("STREAM_FPS", "8"))
STREAM_Q = int(os.environ.get("STREAM_Q", "7"))
ADB = os.environ.get("ADB_BIN", "adb")
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
MAX_UPLOAD = 500 * 1024 * 1024  # APK 上限 500MB
START_TIME = time.time()

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(os.environ.get("APP_TZ", "Asia/Shanghai"))
except Exception:  # noqa: BLE001
    TZ = timezone(timedelta(hours=8))

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

KEY_EVENTS = {
    "BACK": "KEYCODE_BACK",
    "HOME": "KEYCODE_HOME",
    "APP_SWITCH": "KEYCODE_APP_SWITCH",
    "POWER": "KEYCODE_POWER",
    "VOLUME_UP": "KEYCODE_VOLUME_UP",
    "VOLUME_DOWN": "KEYCODE_VOLUME_DOWN",
    "ENTER": "KEYCODE_ENTER",
    "MENU": "KEYCODE_MENU",
}

ADB_LOCK = threading.Lock()


# ------------------------------------------------------------------ ADB


def adb(args, timeout=20, input_data=None):
    """执行 adb 命令，返回 (rc, stdout, stderr)。调用方需注意并发（外部有锁）。"""
    cmd = [ADB, "-s", ADB_SERIAL] + args
    try:
        proc = subprocess.run(
            cmd,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", "replace").strip(),
            proc.stderr.decode("utf-8", "replace").strip(),
        )
    except FileNotFoundError:
        return 127, "", "找不到 adb 命令"
    except subprocess.TimeoutExpired:
        return 124, "", "adb 命令超时（%ss）" % timeout


def adb_shell(args, timeout=20):
    with ADB_LOCK:
        return adb(["shell"] + args, timeout=timeout)


def adb_connect():
    """连接 redroid 设备；返回是否在线。"""
    with ADB_LOCK:
        adb(["connect", ADB_SERIAL], timeout=8)
        rc, out, _ = adb(["devices"], timeout=8)
    return ADB_SERIAL in out and "\tdevice" in out


def device_prop(name):
    rc, out, _ = adb_shell(["getprop", name], timeout=8)
    return out if rc == 0 else ""


def device_resolution():
    rc, out, _ = adb_shell(["wm", "size"], timeout=8)
    match = re.search(r"(\d+)x(\d+)", out)
    if not match:
        return 720, 1280
    return int(match.group(1)), int(match.group(2))


def third_party_packages():
    rc, out, _ = adb_shell(["pm", "list", "packages", "-3"], timeout=15)
    if rc != 0:
        return []
    return sorted(line.split(":", 1)[1] for line in out.splitlines() if ":" in line)


# ------------------------------------------------------------------ 画面流


class FrameHub:
    """把 ffmpeg 输出的 MJPEG 帧广播给所有浏览器连接。

    - 第一个客户端接入时启动采集管线，最后一个断开后 15 秒停止，省 CPU
    - 每个客户端只取「最新一帧」，慢客户端自动丢帧，不拖慢整体
    """

    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.clients = 0
        self.running = False
        self.mode = ""  # screenrecord / screencap
        self.error = ""
        self.stop_at = 0.0
        self.thread = None
        self.lock = threading.Lock()
        self.seed_lock = threading.Lock()
        self.last_publish = 0.0
        self.last_refresh = 0.0

    # ---- 采集线程 ----

    def _publish(self, frame):
        with self.cond:
            self.frame = frame
            self.seq += 1
            self.last_publish = time.time()
            self.cond.notify_all()

    def _set_error(self, message):
        with self.cond:
            self.error = message
            self.cond.notify_all()

    def _read_mjpeg(self, stream):
        """从 ffmpeg stdout 里按 JPEG 起止标记切帧。"""
        buf = b""
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    buf = buf[start:]
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                self._publish(frame)

    def _seed_frame(self):
        """先截一帧静态画面，避免屏幕静止时（screenrecord 不产帧）浏览器一直黑屏。"""
        try:
            with ADB_LOCK:
                cap = subprocess.run(
                    [ADB, "-s", ADB_SERIAL, "exec-out", "screencap", "-p"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                )
            if not cap.stdout:
                return
            conv = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error",
                 "-f", "png_pipe", "-i", "pipe:0", "-frames:v", "1",
                 "-vf", "scale=%d:-2" % STREAM_W, "-q:v", str(STREAM_Q),
                 "-f", "mjpeg", "pipe:1"],
                input=cap.stdout, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=30,
            )
            self._read_mjpeg_from_bytes(conv.stdout)
        except Exception as exc:  # noqa: BLE001
            self._set_error("首帧截图失败：%s" % exc)

    def _pipeline_screenrecord(self):
        """screenrecord 单次最长 180s，循环重启；逐帧转 MJPEG（不额外节流）。"""
        size = "%dx%d" % device_resolution()
        adb_cmd = [
            ADB, "-s", ADB_SERIAL, "exec-out", "screenrecord",
            "--output-format=h264", "--bit-rate", "4M",
            "--size", size, "--time-limit", "179", "-",
        ]
        ffmpeg_cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            # screenrecord 的裸 H.264 没有时间戳，genpts 让解码器自己造
            "-fflags", "+genpts", "-f", "h264", "-i", "pipe:0", "-an",
            "-vf", "scale=%d:-2" % STREAM_W,
            "-q:v", str(STREAM_Q), "-flush_packets", "1", "-f", "mjpeg", "pipe:1",
        ]
        screen = subprocess.Popen(adb_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        transcode = subprocess.Popen(ffmpeg_cmd, stdin=screen.stdout,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        screen.stdout.close()
        try:
            self._read_mjpeg(transcode.stdout)
        finally:
            for proc in (transcode, screen):
                try:
                    proc.kill()
                except OSError:
                    pass

    def _pipeline_screencap(self):
        """兜底：screenrecord 不可用时，按 STREAM_FPS 抓帧截图转 MJPEG。"""
        ffmpeg_cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "png_pipe", "-i", "pipe:0", "-vf",
            "scale=%d:-2" % STREAM_W, "-q:v", str(STREAM_Q),
            "-flush_packets", "1", "-f", "mjpeg", "pipe:1",
        ]
        interval = max(0.15, 1.0 / max(1, STREAM_FPS))
        while self.running:
            with ADB_LOCK:
                proc = subprocess.run(
                    [ADB, "-s", ADB_SERIAL, "exec-out", "screencap", "-p"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                )
            if proc.stdout:
                out = subprocess.run(ffmpeg_cmd, input=proc.stdout,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     timeout=30)
                self._read_mjpeg_from_bytes(out.stdout)
            time.sleep(interval)

    def _read_mjpeg_from_bytes(self, data):
        buf = data
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                return
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                return
            self._publish(buf[start:end + 2])
            buf = buf[end + 2:]

    def _supervise(self):
        self.running = True
        self._seed_frame()  # 先给浏览器一帧静态画面
        while self.running:
            if td := self.stop_at:
                if time.time() > td:
                    break
            try:
                self.mode = "screenrecord"
                started = time.time()
                self._pipeline_screenrecord()
                if time.time() - started < 10:
                    raise RuntimeError("screenrecord 管线很快退出")
            except Exception as exc:  # noqa: BLE001
                self._set_error("screenrecord 不可用，改用逐帧截图：%s" % exc)
                self.mode = "screencap"
                try:
                    self._pipeline_screencap()
                except Exception as exc2:  # noqa: BLE001
                    self._set_error("截图兜底也失败：%s" % exc2)
                    time.sleep(2)
            if not self.running:
                break
        self.running = False
        self.mode = ""

    def _start(self):
        with self.lock:
            if self.running:
                return
            self.stop_at = 0.0
            self.thread = threading.Thread(target=self._supervise, daemon=True)
            self.thread.start()

    def _stop_later(self):
        with self.lock:
            if self.clients > 0:
                return
            if not self.running:
                return
            self.stop_at = time.time() + 15

    # ---- 客户端接口 ----

    def attach(self):
        with self.cond:
            self.clients += 1
        self._start()
        # 管线可能还在（最后一个客户端断开后保留 15 秒），此时 HUB 里可能是旧画面；
        # 屏幕静止时 screenrecord 也不会产新帧，所以给新客户端补一张当前截图。
        if self.running and (time.time() - self.last_publish > 2):
            threading.Thread(target=self._refresh_seed, daemon=True).start()

    def _refresh_seed(self):
        if not self.seed_lock.acquire(blocking=False):
            return
        try:
            if time.time() - self.last_publish < 1.5:
                return
            self._seed_frame()
        finally:
            self.seed_lock.release()

    def request_refresh(self, delays=(0.8, 2.0)):
        """输入注入后补「操作完成后的最终画面」。

        screenrecord 只在画面变化时出帧，动画结束时的那一帧经常抓不到，
        浏览器就会停在动画中间。这里在输入后分两次截图（动画中 + 动画后），
        保证最终状态准确。
        """
        now = time.time()
        with self.lock:
            if now - self.last_refresh < 0.6:
                return
            self.last_refresh = now

        def run():
            previous = 0.0
            for delay in delays:
                time.sleep(max(0.05, delay - previous))
                previous = delay
                with self.seed_lock:
                    self._seed_frame()

        threading.Thread(target=run, daemon=True).start()

    def detach(self):
        with self.cond:
            self.clients = max(0, self.clients - 1)
        self._stop_later()

    def latest(self, last_seq, timeout=10):
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            if self.seq == last_seq:
                return None, last_seq
            return self.frame, self.seq

    def status(self):
        with self.cond:
            return {
                "clients": self.clients,
                "streaming": self.running,
                "mode": self.mode,
                "error": self.error,
                "seq": self.seq,
            }


HUB = FrameHub()


# ------------------------------------------------------------------ HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "android-box/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础响应 ----

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if content_type.startswith("application/json") or content_type.startswith("multipart"):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _json(self, status, payload, head_only=False):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", head_only)

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _read_json(self, limit=65536):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "Content-Length 不合法"
        if length <= 0:
            return None, "请求体为空"
        if length > limit:
            return None, "请求体过大"
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "请求体不是合法 JSON"
        return payload, None

    # ---- 路由 ----

    def do_GET(self):
        self._route(head_only=False)

    def do_HEAD(self):
        self._route(head_only=True)

    def _route(self, head_only=False):
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon", head_only)
        if path == "/stream.mjpg":
            return self._stream()
        if path == "/api/health":
            return self._json(200, {
                "ok": True, "service": "android-box",
                "time": datetime.now(TZ).isoformat(timespec="seconds"),
                "uptime": int(time.time() - START_TIME),
            }, head_only)
        if path == "/api/state":
            return self._state(head_only)
        if path == "/api/apps":
            return self._json(200, {"packages": third_party_packages()}, head_only)
        static = STATIC_FILES.get(path)
        if static is None:
            return self._error(404, "页面不存在")
        filename, content_type = static
        try:
            with open(os.path.join(BASE_DIR, filename), "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error(500, "静态文件缺失：%s" % filename)
        self._send(200, body, content_type, head_only)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/input":
            return self._input()
        if path == "/api/install":
            return self._install()
        self._error(404, "接口不存在")

    def do_PUT(self):
        self._error(405, "不支持的请求方法")

    def do_DELETE(self):
        self._error(405, "不支持的请求方法")

    # ---- 状态 ----

    def _state(self, head_only=False):
        online = adb_connect()
        payload = {
            "ok": True,
            "adb_online": online,
            "serial": ADB_SERIAL,
            "stream": HUB.status(),
            "uptime": int(time.time() - START_TIME),
        }
        if online:
            width, height = device_resolution()
            payload.update({
                "model": device_prop("ro.product.model") or device_prop("ro.product.device"),
                "android": device_prop("ro.build.version.release"),
                "sdk": device_prop("ro.build.version.sdk"),
                "resolution": {"width": width, "height": height},
                "booted": device_prop("sys.boot_completed") == "1",
            })
        self._json(200, payload, head_only)

    # ---- MJPEG 流 ----

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        HUB.attach()
        last_seq = -1
        last_frame = None
        try:
            while True:
                # 2 秒没有新帧就重发上一帧：MJPEG 需要下一段边界才会绘出首帧，
                # 这也顺便探测客户端是否已断开
                frame, seq = HUB.latest(last_seq, timeout=2)
                if frame is None:
                    if last_frame is not None:
                        self._write_frame(last_frame)
                    continue
                last_frame = frame
                last_seq = seq
                self._write_frame(frame)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.detach()

    def _write_frame(self, frame):
        header = (
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(frame)).encode() + b"\r\n\r\n"
        )
        self.wfile.write(header + frame + b"\r\n")
        self.wfile.flush()

    # ---- 输入注入 ----

    def _input(self):
        payload, err = self._read_json()
        if err:
            return self._error(400, err)
        kind = payload.get("type")
        if kind == "tap":
            x, y = int(payload.get("x", 0)), int(payload.get("y", 0))
            rc, _, err = adb_shell(["input", "tap", str(x), str(y)], timeout=15)
        elif kind == "swipe":
            args = [str(int(payload.get(k, 0))) for k in ("x1", "y1", "x2", "y2")]
            duration = max(50, min(3000, int(payload.get("duration", 200))))
            rc, _, err = adb_shell(["input", "swipe"] + args + [str(duration)], timeout=20)
        elif kind == "key":
            key = KEY_EVENTS.get(str(payload.get("key", "")).upper())
            if not key:
                return self._error(400, "不支持的按键")
            rc, _, err = adb_shell(["input", "keyevent", key], timeout=15)
        elif kind == "text":
            text = str(payload.get("text", ""))
            if not text or len(text) > 200:
                return self._error(400, "文字长度需在 1~200 之间")
            # adb input text 只支持 ASCII，空格用 %s 转义
            safe = "".join(ch if 32 <= ord(ch) < 127 else "" for ch in text)
            safe = safe.replace(" ", "%s")
            if not safe:
                return self._error(400, "仅支持 ASCII 文字输入（中文请用设备软键盘）")
            rc, _, err = adb_shell(["input", "text", safe], timeout=15)
        else:
            return self._error(400, "未知输入类型")
        if rc != 0:
            return self._error(502, "注入失败：%s" % err)
        # 注入后补一张最终画面（screenrecord 在动画结束时不一定出新帧）
        HUB.request_refresh()
        self._json(200, {"ok": True})

    # ---- APK 安装 ----

    def _install(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "Content-Length 不合法")
        if length <= 0:
            return self._error(400, "请求体为空")
        if length > MAX_UPLOAD:
            return self._error(413, "APK 超过 500MB 上限")
        tmp_path = os.path.join(tempfile.gettempdir(), "upload-%d.apk" % int(time.time() * 1000))
        remaining = length
        try:
            with open(tmp_path, "wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise IOError("上传中断")
                    fh.write(chunk)
                    remaining -= len(chunk)
            rc, out, err = adb(["install", "-r", "-g", tmp_path], timeout=300)
        except IOError as exc:
            return self._error(400, "上传失败：%s" % exc)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        ok = rc == 0 and "Success" in out
        self._json(200 if ok else 502, {
            "ok": ok,
            "output": (out + "\n" + err).strip()[:2000],
        })

    # ---- 日志 ----

    def log_message(self, fmt, *args):
        sys.stderr.write(
            "[android-box] %s %s\n" % (datetime.now(TZ).strftime("%H:%M:%S"), fmt % args)
        )


def adb_watchdog():
    """后台保活：确保 adb 连上 redroid。"""
    while True:
        try:
            adb_connect()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(10)


def main():
    print("[android-box] serving on http://%s:%d  (adb serial: %s)" % (HOST, PORT, ADB_SERIAL),
          flush=True)
    if not shutil.which(ADB):
        print("[android-box] 警告：找不到 adb 命令，画面与输入不可用", file=sys.stderr)
    if not shutil.which(FFMPEG):
        print("[android-box] 警告：找不到 ffmpeg 命令，画面不可用", file=sys.stderr)
    threading.Thread(target=adb_watchdog, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[android-box] bye", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
