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

import io
import json
import os
import re
import shutil
import socket
import struct
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

# 低延迟输入注入：预置 scrcpy-server（Apache-2.0, © Genymobile）常驻在设备里，
# 通过本地 socket 发送注入指令，避免每次 adb shell input 都冷启动 Java 进程（~700ms → ~30ms）
SCRCPY_JAR = os.path.join(BASE_DIR, "assets", "scrcpy-server-v2.4")
DEVICE_JAR = "/data/local/tmp/scrcpy-server.jar"
SCRCPY_PORT = int(os.environ.get("SCRCPY_PORT", "27183"))

# 画面：默认用并行 raw 截图轮询（每帧都是最新截图，延迟稳定 ~200ms）；
# 没装 Pillow 时自动退回 screenrecord+ffmpeg 的 MJPEG 管线。
try:
    from PIL import Image  # python3-pil（系统包，非 pip 依赖）

    HAS_PIL = True
except Exception:  # noqa: BLE001
    HAS_PIL = False
CAPTURE_WORKERS = max(1, min(4, int(os.environ.get("CAPTURE_WORKERS", "2"))))
JPEG_QUALITY = max(20, min(95, int(os.environ.get("JPEG_QUALITY", "72"))))
PIXEL_FORMATS = {1: "RGBA", 2: "RGBX", 3: "RGB", 4: "BGR;16", 5: "BGRA"}

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

# 同名按键对应的 Android keycode（scrcpy 注入通道用）
KEYCODES = {
    "BACK": 4,
    "HOME": 3,
    "APP_SWITCH": 187,
    "POWER": 26,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "ENTER": 66,
    "MENU": 82,
}

# MotionEvent 常量
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2
BUTTON_PRIMARY = 1

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


def capture_jpeg_raw():
    """一次原始截图 → Pillow 转 JPEG（~200ms/帧，不经过 H.264 编码器，所以没有编码器排队延迟）。"""
    if not HAS_PIL:
        return None
    proc = subprocess.run(
        [ADB, "-s", ADB_SERIAL, "exec-out", "screencap"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20,
    )
    raw = proc.stdout
    if len(raw) < 12:
        return None
    width, height, fmt = struct.unpack("<III", raw[:12])
    if width <= 0 or height <= 0:
        return None
    pixels = raw[12:]
    expected = width * height * 4
    if len(pixels) < expected:
        return None
    img = Image.frombytes(PIXEL_FORMATS.get(fmt, "RGBA"), (width, height), pixels).convert("RGB")
    if width != STREAM_W:
        target_h = max(1, round(height * STREAM_W / width))
        img = img.resize((STREAM_W, target_h), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


# ------------------------------------------------------------------ 低延迟注入


class ScrcpyInjector:
    """常驻注入通道：一次启动 scrcpy-server，之后每次触摸/按键只写几十字节。

    对比 `adb shell input`：后者每次都要冷启动一个 Java 进程（本机实测 700~800ms），
    常驻通道实测 ~30ms。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.sock = None
        self.width, self.height = 480, 854
        self.last_error = ""
        self.warming = False

    @property
    def ready(self):
        return self.sock is not None

    def warm_async(self):
        """后台预热注入通道，避免第一个请求被 2~3 秒的初始化阻塞。"""
        if self.sock is not None:
            return True
        with self.lock:
            if self.warming:
                return False
            self.warming = True

        def run():
            try:
                self.ensure()
            finally:
                with self.lock:
                    self.warming = False

        threading.Thread(target=run, daemon=True).start()
        return False

    def ensure(self):
        with self.lock:
            if self.sock is not None:
                return True
            try:
                self._setup_locked()
                self.last_error = ""
                return True
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self._close_locked()
                return False

    def _adb_locked(self, args, timeout=20):
        with ADB_LOCK:
            return adb(args, timeout=timeout)

    def _setup_locked(self):
        # 1) 设备上没有 jar 就推送（容器重建后 /data/local/tmp 会清空）
        rc, _, _ = self._adb_locked(["shell", "test", "-f", DEVICE_JAR], timeout=8)
        if rc != 0:
            if not os.path.exists(SCRCPY_JAR):
                raise RuntimeError("缺少 scrcpy-server：%s" % SCRCPY_JAR)
            rc, _, err = self._adb_locked(["push", SCRCPY_JAR, DEVICE_JAR], timeout=60)
            if rc != 0:
                raise RuntimeError("推送 scrcpy-server 失败：%s" % err)

        # 2) 重启注入服务
        self._adb_locked(["shell", "pkill", "-f", "com.genymobile.scrcpy.Server"], timeout=8)
        time.sleep(0.2)
        cmd = (
            "CLASSPATH=%s app_process / com.genymobile.scrcpy.Server 2.4 "
            "log_level=error video=false audio=false control=true tunnel_forward=true "
            "send_device_meta=true send_dummy_byte=true cleanup=false "
            ">/data/local/tmp/scrcpy.log 2>&1 &" % DEVICE_JAR
        )
        self._adb_locked(["shell", cmd], timeout=10)
        time.sleep(1.2)

        # 3) adb forward + 握手（1 字节 dummy + 64 字节设备名）
        self._adb_locked(["forward", "tcp:%d" % SCRCPY_PORT, "localabstract:scrcpy"], timeout=8)
        sock = socket.create_connection(("127.0.0.1", SCRCPY_PORT), timeout=6)
        sock.settimeout(6)
        dummy = sock.recv(1)
        if dummy != b"\x00":
            sock.close()
            raise RuntimeError("scrcpy 握手失败（dummy=%r）" % dummy)
        meta = b""
        while len(meta) < 64:
            chunk = sock.recv(64 - len(meta))
            if not chunk:
                break
            meta += chunk
        self.sock = sock
        self.width, self.height = device_resolution()

    def _close_locked(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _write(self, payload):
        if self.sock is None:
            raise RuntimeError("注入通道未就绪")
        try:
            self.sock.sendall(payload)
        except OSError:
            self._close_locked()
            raise

    def _touch(self, action, x, y):
        pressure = 0xFFFF if action != ACTION_UP else 0
        action_button = BUTTON_PRIMARY if action == ACTION_DOWN else 0
        buttons = 0 if action == ACTION_UP else BUTTON_PRIMARY
        self._write(struct.pack(
            ">BBqiiHHHii", 2, action, 0x12345678, int(x), int(y),
            self.width, self.height, pressure, action_button, buttons,
        ))

    def tap(self, x, y):
        with self.lock:
            self._touch(ACTION_DOWN, x, y)
            self._touch(ACTION_UP, x, y)

    def swipe(self, x1, y1, x2, y2, duration):
        with self.lock:
            steps = max(3, min(24, int(duration / 40)))
            self._touch(ACTION_DOWN, x1, y1)
            for i in range(1, steps + 1):
                x = x1 + (x2 - x1) * i / steps
                y = y1 + (y2 - y1) * i / steps
                time.sleep(max(0.008, duration / 1000.0 / steps))
                self._touch(ACTION_MOVE, int(x), int(y))
            self._touch(ACTION_UP, x2, y2)

    def key(self, name):
        keycode = KEYCODES.get(name.upper())
        if keycode is None:
            raise ValueError("不支持的按键")
        with self.lock:
            self._write(struct.pack(">BBiii", 0, ACTION_DOWN, keycode, 0, 0))
            self._write(struct.pack(">BBiii", 0, ACTION_UP, keycode, 0, 0))

    def text(self, text):
        data = text.encode("utf-8")
        with self.lock:
            self._write(struct.pack(">BI", 1, len(data)) + data)


INJECTOR = ScrcpyInjector()


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
        """先截一帧静态画面，避免浏览器黑屏。优先走 raw+Pillow 快路径（~200ms）。"""
        try:
            if HAS_PIL:
                jpeg = capture_jpeg_raw()
                if jpeg:
                    self._publish(jpeg)
                    return
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

    def _poll_worker(self):
        """raw 截图轮询：每帧都是「当前屏幕」的即时截图，没有编码器排队延迟。"""
        while self.running:
            if self.stop_at and time.time() > self.stop_at:
                break
            try:
                jpeg = capture_jpeg_raw()
                if jpeg:
                    self._publish(jpeg)
                else:
                    time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001
                self._set_error("截图失败：%s" % exc)
                time.sleep(0.5)
        if self.mode == "poll" and not self.running:
            return

    def _supervise_poll(self):
        """并行 raw 截图（默认 2 路 ≈ 7fps），每帧延迟稳定 ~200ms。"""
        self.mode = "poll"
        threads = [
            threading.Thread(target=self._poll_worker, daemon=True)
            for _ in range(CAPTURE_WORKERS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def _supervise(self):
        self.running = True
        try:
            if HAS_PIL:
                self._supervise_poll()
            else:
                self._seed_frame()  # 无 Pillow 时退回 ffmpeg 管线，先给一帧静态画面
                while self.running:
                    if self.stop_at and time.time() > self.stop_at:
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
        finally:
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
        if self.mode == "poll":
            return  # 轮询模式下每帧都是最新截图，无需补帧
        if not self.seed_lock.acquire(blocking=False):
            return
        try:
            if time.time() - self.last_publish < 1.5:
                return
            self._seed_frame()
        finally:
            self.seed_lock.release()

    def request_refresh(self):
        """输入注入后按需补「操作完成后的最终画面」。

        轮询模式（默认）每帧都是即时截图，不需要补帧；只有退回 ffmpeg 管线时才补。
        """
        if self.mode == "poll":
            return
        injected_at = time.time()
        with self.lock:
            if injected_at - self.last_refresh < 0.25:
                return
            self.last_refresh = injected_at

        def run():
            time.sleep(0.35)
            if self.last_publish < injected_at:
                with self.seed_lock:
                    self._seed_frame()
            baseline = time.time()
            time.sleep(0.9)
            # 动画已结束且之后没有新帧 → 补一张最终状态
            if self.last_publish < baseline:
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
        if kind not in ("tap", "swipe", "key", "text"):
            return self._error(400, "未知输入类型")

        # 参数校验（两种注入通道共用）
        if kind == "key":
            key_name = str(payload.get("key", "")).upper()
            if key_name not in KEYCODES:
                return self._error(400, "不支持的按键")
        if kind == "text":
            text = str(payload.get("text", ""))
            if not text or len(text) > 200:
                return self._error(400, "文字长度需在 1~200 之间")
            # scrcpy / adb 都只支持 ASCII；中文请用设备软键盘
            safe_text = "".join(ch if 32 <= ord(ch) < 127 else "" for ch in text)
            if not safe_text:
                return self._error(400, "仅支持 ASCII 文字输入（中文请用设备软键盘）")
        if kind == "swipe":
            duration = max(50, min(3000, int(payload.get("duration", 200))))

        via = "scrcpy"
        try:
            if not INJECTOR.ready:
                # 不阻塞请求：后台预热，本次先用 adb 回退（后续请求走常驻通道）
                INJECTOR.warm_async()
                raise RuntimeError("注入通道预热中")
            if kind == "tap":
                INJECTOR.tap(int(payload.get("x", 0)), int(payload.get("y", 0)))
            elif kind == "swipe":
                INJECTOR.swipe(
                    int(payload.get("x1", 0)), int(payload.get("y1", 0)),
                    int(payload.get("x2", 0)), int(payload.get("y2", 0)),
                    duration,
                )
            elif kind == "key":
                INJECTOR.key(key_name)
            else:
                INJECTOR.text(safe_text)
        except Exception:  # noqa: BLE001 — 常驻通道异常时回退到 adb input
            via = "adb"
            rc, _, err = self._adb_input_fallback(kind, payload, safe_text if kind == "text" else "")
            if rc != 0:
                return self._error(502, "注入失败：%s" % err)

        # 注入后补一张最终画面（screenrecord 在动画结束时不一定出新帧）
        HUB.request_refresh()
        self._json(200, {"ok": True, "via": via})

    def _adb_input_fallback(self, kind, payload, safe_text):
        if kind == "tap":
            return adb_shell(["input", "tap", str(int(payload.get("x", 0))), str(int(payload.get("y", 0)))], timeout=15)
        if kind == "swipe":
            args = [str(int(payload.get(k, 0))) for k in ("x1", "y1", "x2", "y2")]
            duration = max(50, min(3000, int(payload.get("duration", 200))))
            return adb_shell(["input", "swipe"] + args + [str(duration)], timeout=20)
        if kind == "key":
            return adb_shell(["input", "keyevent", KEY_EVENTS[str(payload.get("key", "")).upper()]], timeout=15)
        return adb_shell(["input", "text", safe_text.replace(" ", "%s")], timeout=15)

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
    """后台保活：确保 adb 连上 redroid，并预热低延迟注入通道。"""
    while True:
        try:
            if adb_connect() and not INJECTOR.ready:
                # 设备重启/容器重建后 socket 会失效；这里自动重推 jar、重启服务
                if INJECTOR.ensure():
                    print("[android-box] scrcpy 注入通道就绪", flush=True)
                else:
                    print("[android-box] scrcpy 注入通道未就绪：%s" % INJECTOR.last_error,
                          file=sys.stderr, flush=True)
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
