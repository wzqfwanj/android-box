/* ============================================================
   安卓系统 · 在线真机 — 交互
   ------------------------------------------------------------
   为什么需要 JS：页面要展示设备实时画面（MJPEG）、把点按/拖动
   换算成设备坐标并通过后端注入到真实 Android、上传安装 APK。
   这些交互无法用纯 HTML/CSS 完成，因此引入一份原生 app.js，
   不引用任何框架或外部资源。
   ============================================================ */

(function () {
  "use strict";

  var $ = function (sel) {
    return document.querySelector(sel);
  };

  var state = {
    device: null,
    renderer: null, // "ws"（WebCodecs 直连） / "mjpeg" / null
    ws: null,
    decoder: null,
    wsConfig: null,
    frameTs: 0,
    lastFrameAt: 0,
    wsFrames: 0,
    decodeErrors: 0,
    lastError: "",
    ratio: "9 / 16",
    lastTapAt: 0,
  };
  // 便于线上诊断（只读状态，不影响功能）
  window.__androidBox = state;

  var toastTimer = 0;

  function toast(message) {
    var el = $("#toast");
    if (!el) return;
    el.textContent = message;
    el.classList.add("is-show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.classList.remove("is-show");
    }, 2400);
  }

  // 点击/按下即时涟漪：真实画面要等下一帧截图（~150-250ms），
  // 先用本地涟漪给出「已收到操作」的即时反馈。
  function showRipple(clientX, clientY) {
    var el = document.createElement("span");
    el.className = "tap-ripple";
    el.style.left = clientX + "px";
    el.style.top = clientY + "px";
    document.body.appendChild(el);
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 320);
  }

  function setStatus(text) {
    var el = $("#status");
    if (el) el.textContent = text;
  }

  function api(path, options) {
    return fetch(path, options || {}).then(function (res) {
      return res
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
          return data;
        });
    });
  }

  function resolution() {
    var res = state.device && state.device.resolution;
    if (res && res.width && res.height) return res;
    return { width: 720, height: 1280 };
  }

  // ---------------------------------------------------------- 画面流

  // 优先：WebSocket 收 scrcpy 的 H.264 裸流，浏览器用 WebCodecs 直接解码
  // （去掉服务端 ffmpeg 转码这一跳，端到端约 10~80ms）。
  // 回退：同源 MJPEG <img>（老浏览器 / 解码器初始化失败时）。

  function supportsWebCodecs() {
    return typeof window.VideoDecoder === "function" && typeof window.WebSocket === "function";
  }

  function wsVideoUrl() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var dir = location.pathname.replace(/[^/]*$/, "");
    return proto + "//" + location.host + dir + "ws/video";
  }

  function concatBytes(a, b) {
    var out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  function hideBoot() {
    var boot = $("#boot");
    if (boot && !boot.classList.contains("is-hidden")) boot.classList.add("is-hidden");
  }

  function startWebCodecs() {
    if (state.renderer) return;
    var canvas = $("#screenCanvas");
    var img = $("#frame");
    if (!canvas || !img) return;
    var ctx = canvas.getContext("2d", { alpha: false });
    var decoder;
    try {
      decoder = new VideoDecoder({
        output: function (frame) {
          try {
            if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
              canvas.width = frame.displayWidth;
              canvas.height = frame.displayHeight;
            }
            ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
          } catch (err) {
            /* 单帧绘制失败忽略 */
          }
          frame.close();
          hideBoot();
          state.lastFrameAt = Date.now();
        },
        error: function (err) {
          state.decodeErrors += 1;
          state.lastError = err && err.message ? err.message : String(err);
          toast("视频解码失败：" + state.lastError);
          fallbackToMjpeg();
        },
      });
      // Annex-B 裸流：不传 description，SPS/PPS 随配置包在带内发送
      decoder.configure({ codec: "avc1.42E01E", optimizeForLatency: true });
    } catch (err) {
      attachMjpeg();
      return;
    }

    var ws;
    try {
      ws = new WebSocket(wsVideoUrl());
    } catch (err) {
      try {
        decoder.close();
      } catch (err2) {
        /* ignore */
      }
      attachMjpeg();
      return;
    }
    ws.binaryType = "arraybuffer";
    state.renderer = "ws";
    state.ws = ws;
    state.decoder = decoder;
    state.wsConfig = null;
    state.frameTs = 0;
    canvas.hidden = false;
    img.hidden = true;

    // 首帧占位：编码器关键帧可能还要等一会儿，先用一张截图把画面显示出来
    var snapshot = new Image();
    snapshot.onload = function () {
      if (state.renderer !== "ws") return;
      try {
        canvas.width = snapshot.naturalWidth || 480;
        canvas.height = snapshot.naturalHeight || 854;
        ctx.drawImage(snapshot, 0, 0, canvas.width, canvas.height);
        hideBoot();
      } catch (err) {
        /* ignore */
      }
    };
    snapshot.src = "api/snapshot?ts=" + Date.now();

    ws.onmessage = function (event) {
      if (!(event.data instanceof ArrayBuffer)) return;
      state.wsFrames += 1;
      var bytes = new Uint8Array(event.data);
      if (bytes.length < 2) return;
      var flags = bytes[0];
      var payload = bytes.subarray(1);
      if (flags & 1) {
        // 配置包（SPS/PPS）：拼到下一帧前面一起喂给解码器
        state.wsConfig = payload;
        return;
      }
      var type = (flags & 2) ? "key" : "delta";
      var data = payload;
      if (state.wsConfig) {
        data = concatBytes(state.wsConfig, payload);
        state.wsConfig = null;
        type = "key";
      }
      state.frameTs += 33;
      try {
        state.decoder.decode(new EncodedVideoChunk({
          type: type,
          timestamp: state.frameTs,
          data: data,
        }));
      } catch (err) {
        fallbackToMjpeg();
      }
    };
    ws.onerror = function () {
      fallbackToMjpeg();
    };
    ws.onclose = function () {
      if (!document.hidden && state.renderer === "ws") fallbackToMjpeg();
    };
  }

  function attachMjpeg() {
    var img = $("#frame");
    var canvas = $("#screenCanvas");
    if (!img) return;
    if (canvas) canvas.hidden = true;
    state.renderer = "mjpeg";
    img.hidden = false;
    img.src = "stream.mjpg?ts=" + Date.now();
    img.onerror = function () {
      if (document.hidden) return;
      setStatus("画面流断开，2 秒后重连…");
      setTimeout(function () {
        if (!document.hidden && state.renderer === "mjpeg") attachMjpeg();
      }, 2000);
    };
  }

  function fallbackToMjpeg() {
    stopStream();
    attachMjpeg();
  }

  function stopStream() {
    state.renderer = null;
    if (state.ws) {
      try {
        state.ws.close();
      } catch (err) {
        /* ignore */
      }
      state.ws = null;
    }
    if (state.decoder) {
      try {
        state.decoder.close();
      } catch (err) {
        /* ignore */
      }
      state.decoder = null;
    }
    state.wsConfig = null;
    var img = $("#frame");
    if (img) {
      img.onerror = null;
      img.removeAttribute("src");
    }
  }

  function attachStream() {
    if (state.renderer) return;
    if (supportsWebCodecs()) startWebCodecs();
    else attachMjpeg();
  }

  function detachStream() {
    stopStream();
  }

  // ---------------------------------------------------------- 触摸映射

  function toDevice(clientX, clientY) {
    var screenEl = $("#screen");
    var rect = screenEl.getBoundingClientRect();
    var res = resolution();
    var x = Math.round(((clientX - rect.left) / rect.width) * res.width);
    var y = Math.round(((clientY - rect.top) / rect.height) * res.height);
    return {
      x: Math.min(Math.max(x, 0), res.width - 1),
      y: Math.min(Math.max(y, 0), res.height - 1),
    };
  }

  function postInput(payload) {
    return api("api/input", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(function (err) {
      toast("输入失败：" + err.message);
    });
  }

  function sendTap(clientX, clientY) {
    showRipple(clientX, clientY);
    var point = toDevice(clientX, clientY);
    postInput({ type: "tap", x: point.x, y: point.y });
  }

  function sendSwipe(x1, y1, x2, y2, duration) {
    showRipple(x1, y1);
    var a = toDevice(x1, y1);
    var b = toDevice(x2, y2);
    postInput({ type: "swipe", x1: a.x, y1: a.y, x2: b.x, y2: b.y, duration: duration });
  }

  function bindTouch() {
    var screenEl = $("#screen");
    if (!screenEl) return;
    var gesture = { active: false, x: 0, y: 0, t: 0, moved: false };

    screenEl.addEventListener("pointerdown", function (event) {
      if (event.button != null && event.button !== 0) return;
      gesture.active = true;
      gesture.x = event.clientX;
      gesture.y = event.clientY;
      gesture.t = Date.now();
      gesture.moved = false;
      try {
        screenEl.setPointerCapture(event.pointerId);
      } catch (err) {
        /* 某些浏览器不支持，忽略 */
      }
    });

    screenEl.addEventListener("pointermove", function (event) {
      if (!gesture.active) return;
      if (Math.abs(event.clientX - gesture.x) > 8 || Math.abs(event.clientY - gesture.y) > 8) {
        gesture.moved = true;
      }
    });

    function finish(event) {
      if (!gesture.active) return;
      gesture.active = false;
      var duration = Date.now() - gesture.t;
      if (!gesture.moved && duration < 600) {
        var now = Date.now();
        if (now - state.lastTapAt < 120) return; // 双击防抖
        state.lastTapAt = now;
        sendTap(event.clientX, event.clientY);
      } else {
        sendSwipe(gesture.x, gesture.y, event.clientX, event.clientY,
                  Math.min(2000, Math.max(80, duration)));
      }
    }

    screenEl.addEventListener("pointerup", finish);
    screenEl.addEventListener("pointercancel", function () {
      gesture.active = false;
    });
    screenEl.addEventListener("contextmenu", function (event) {
      event.preventDefault();
    });
  }

  // ---------------------------------------------------------- 状态轮询

  function paintState() {
    var device = state.device;
    if (!device) {
      setStatus("等待后端…");
      return;
    }
    if (!device.adb_online) {
      setStatus("等待安卓设备上线（redroid 容器启动中）…");
      return;
    }
    var res = resolution();
    var stream = device.stream || {};
    var parts = [];
    parts.push("在线：" + (device.model || "Android"));
    parts.push("Android " + (device.android || "?"));
    parts.push(res.width + "×" + res.height);
    if (!device.booted) parts.push("系统启动中…");
    if (stream.streaming) parts.push("画面 " + (stream.mode === "screencap" ? "逐帧" : "实时"));
    if (state.renderer === "ws") parts.push("视频直连");
    if (stream.error) parts.push(stream.error);
    setStatus(parts.join(" · "));

    if (device.booted) {
      var boot = $("#boot");
      if (boot && !boot.classList.contains("is-hidden")) boot.classList.add("is-hidden");
      if (!state.renderer && !document.hidden) attachStream();
    }
  }

  function pollState() {
    api("api/state")
      .then(function (data) {
        state.device = data;
        if (data && data.resolution) {
          var ratio = data.resolution.width + " / " + data.resolution.height;
          if (ratio !== state.ratio) {
            state.ratio = ratio;
            $("#phone").style.setProperty("--ratio", ratio);
          }
        }
        paintState();
      })
      .catch(function () {
        setStatus("后端连接失败，稍后重试…");
      });
  }

  // ---------------------------------------------------------- 按键与安装

  function bindButtons() {
    document.addEventListener("click", function (event) {
      var btn = event.target.closest("[data-key]");
      if (!btn) return;
      postInput({ type: "key", key: btn.getAttribute("data-key") });
    });

    var reconnect = $("#reconnect");
    if (reconnect) {
      reconnect.addEventListener("click", function () {
        detachStream();
        setTimeout(attachStream, 200);
        toast("正在重连画面");
      });
    }

    var input = $("#apkInput");
    if (input) {
      input.addEventListener("change", function () {
        var file = input.files && input.files[0];
        if (!file) return;
        installApk(file);
        input.value = "";
      });
    }
  }

  function installApk(file) {
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "api/install", true);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    setStatus("上传 APK：" + file.name + "（0%）");
    xhr.upload.onprogress = function (event) {
      if (!event.lengthComputable) return;
      var percent = Math.round((event.loaded / event.total) * 100);
      setStatus("上传 APK：" + file.name + "（" + percent + "%）");
    };
    xhr.upload.onload = function () {
      setStatus("上传完成，正在安装 " + file.name + " …");
    };
    xhr.onload = function () {
      var data = {};
      try {
        data = JSON.parse(xhr.responseText);
      } catch (err) {
        data = {};
      }
      if (xhr.status === 200 && data.ok) {
        toast("APK 安装成功：" + file.name);
        setStatus("APK 安装成功：" + file.name);
      } else {
        var detail = (data && (data.error || data.output)) || ("HTTP " + xhr.status);
        toast("APK 安装失败：" + String(detail).slice(0, 120));
        setStatus("APK 安装失败：" + String(detail).slice(0, 300));
      }
      pollState();
    };
    xhr.onerror = function () {
      toast("APK 上传失败（网络错误）");
      setStatus("APK 上传失败（网络错误）");
    };
    xhr.send(file);
  }

  // ---------------------------------------------------------- 启动

  function bindVisibility() {
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        detachStream(); // 页面不可见时断开，让服务端停掉转码省 CPU
      } else if (state.device && state.device.booted) {
        attachStream();
      }
    });
  }

  function boot() {
    bindTouch();
    bindButtons();
    bindVisibility();
    pollState();
    setInterval(pollState, 3000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
