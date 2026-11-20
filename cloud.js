/* ============================================================
   android-box · 在线真机（ARM 云手机）
   电源按钮：开机 = 创建（含装美团），关机 = 释放
   画面来自 iframe 内阿里云 Web SDK。
   ============================================================ */

(function () {
  "use strict";

  var $ = function (sel) {
    return document.querySelector(sel);
  };

  var state = {
    power: "unknown",
    session: null,
    connected: false,
    sdkRetry: 0,
    pollTimer: null,
    powerText: "",
  };

  // ---- 小工具 ----

  function api(path, options) {
    return fetch(path, Object.assign({ cache: "no-store" }, options || {})).then(function (res) {
      return res.json().then(function (data) {
        data.__status = res.status;
        return data;
      });
    });
  }

  function setStatus(text) {
    var el = $("#status");
    if (el) el.textContent = text;
  }

  function setBoot(title, note) {
    var t = $("#bootTitle");
    var n = $("#bootNote");
    if (t && title) t.textContent = title;
    if (n && note !== undefined) n.textContent = note;
  }

  function showBoot() {
    var boot = $("#boot");
    if (boot) boot.classList.remove("is-hidden");
  }

  function hideBoot() {
    var boot = $("#boot");
    if (boot) boot.classList.add("is-hidden");
  }

  function setPowerText(text) {
    var el = $("#powerText");
    if (el) el.textContent = text || "";
  }

  function renderPower() {
    var btn = $("#powerBtn");
    if (!btn) return;
    var busy = state.power === "creating" || state.power === "releasing";
    btn.disabled = busy || state.power === "unknown";
    if (busy) {
      btn.textContent = state.power === "creating" ? "开机中…" : "关机中…";
    } else if (state.power === "on") {
      btn.textContent = "关机";
    } else if (state.power === "error") {
      btn.textContent = "重试";
    } else {
      btn.textContent = "开机";
    }
    var labels = {
      unknown: "检查中…",
      off: "已关机（释放状态，0 计费）",
      creating: "正在创建云手机并安装美团（约 2~3 分钟）…",
      releasing: "正在释放云手机（停止计费）…",
      on: "已开机",
      error: "操作失败",
    };
    setPowerText(state.powerText || labels[state.power] || "");
  }

  function updatePowerState(payload) {
    if (!payload) return;
    var prev = state.power;
    if (payload.state) state.power = payload.state;
    state.powerText = payload.message || "";
    if (payload.log && payload.log.length) {
      setBoot(state.power === "creating" ? "正在创建云手机…" : "正在释放云手机…",
        payload.log[payload.log.length - 1]);
    }
    renderPower();
    if (payload.state === "on" && prev !== "on") {
      hideBoot();
      setStatus("云手机已就绪");
      connect();
    } else if (payload.state === "off") {
      showBoot();
      setBoot("云手机已关机", "点「开机」自动创建（约 2~3 分钟，含装美团）");
      setStatus("已关机（0 计费）");
      stopSession();
    } else if (payload.state === "creating") {
      showBoot();
      setBoot("正在创建云手机…", payload.message || "创建网络 + 实例 + 安装美团");
      setStatus("开机中…");
      stopSession();
    } else if (payload.state === "releasing") {
      showBoot();
      setBoot("正在释放云手机…", payload.message || "删除实例与网络");
      setStatus("关机中…");
    } else if (payload.state === "error") {
      showBoot();
      setBoot("电源操作失败", payload.message || "见下方状态");
      setStatus("操作失败");
      renderPower();
    }
  }

  // ---- 电源接口 ----

  function fetchPower() {
    return api("api/cp/power")
      .then(function (data) {
        if (data && data.state) updatePowerState(data);
        else {
          state.power = "off";
          renderPower();
        }
      })
      .catch(function () {
        state.power = "error";
        state.powerText = "无法获取电源状态";
        renderPower();
      });
  }

  function pollPower() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(function () {
      fetchPower().finally(function () {
        if (state.power === "creating" || state.power === "releasing") pollPower();
      });
    }, 2500);
  }

  function postPower(action) {
    return api("api/cp/power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action }),
    })
      .then(function (data) {
        if (data.__status === 409) {
          setStatus("操作进行中，请稍候…");
        } else if (data.state) {
          updatePowerState({
            state: data.state,
            message: action === "on" ? "正在创建云手机…" : "正在释放云手机…",
          });
        }
        pollPower();
      })
      .catch(function (err) {
        setStatus("请求失败：" + err.message);
      });
  }

  // ---- 画面串流 ----

  function sdkReady() {
    return window.Wuying && window.Wuying.WebSDK && typeof window.Wuying.WebSDK.createSession === "function";
  }

  function stopSession() {
    state.connected = false;
    if (!state.session) return;
    try {
      if (typeof state.session.stopConnection === "function") state.session.stopConnection();
      else if (typeof state.session.destroy === "function") state.session.destroy();
    } catch (err) { /* 忽略 */ }
    state.session = null;
  }

  function sdkWait() {
    if (sdkReady()) {
      connect();
      return;
    }
    if (state.sdkRetry++ > 40) {
      setStatus("SDK 加载失败，请刷新页面");
      setBoot("SDK 加载失败", "请刷新页面重试");
      return;
    }
    setTimeout(sdkWait, 250);
  }

  function connect() {
    if (state.power !== "on") return;
    if (!sdkReady()) {
      sdkWait();
      return;
    }
    setStatus("获取连接凭证…");
    setBoot("正在连接云手机…", "获取连接凭证 — 云端串流");
    api("api/cp/ticket")
      .then(function (data) {
        if (!data.ok || !data.ticket) {
          var message = data.error || "云手机暂不可用";
          setBoot("云手机暂不可用", message);
          setStatus(message);
          if (message.indexOf("未创建") >= 0 || message.indexOf("已释放") >= 0) {
            state.power = "off";
            renderPower();
          }
          return;
        }
        startSession(data);
      })
      .catch(function (err) {
        setBoot("获取凭证失败", String(err.message || err));
        setStatus("获取凭证失败：" + err.message);
      });
  }

  function startSession(ticketInfo) {
    var connConfig = {
      disableCamera: true,
      decodeType: 2,
      useCustomIme: true,
      disableOrientation: true,
      resolutionAdaptive: false,
    };
    var appInfo = {
      appId: "android",
      appVersion: "",
      appInstanceId: ticketInfo.appInstanceId,
      loginRegionId: "cn-shanghai",
      appName: "云手机",
      productType: "AndroidCloud",
      appInstanceGroupId: ticketInfo.appInstanceGroupId,
      resourceId: "",
      connectionProperties: JSON.stringify({ authMode: "Session" }),
      connConfig: connConfig,
      osType: "Android",
    };
    var sessionParam = {
      openType: "inline",
      iframeId: "sessionIframe",
      resourceType: "local",
      connectType: "app",
      userInfo: { ticket: ticketInfo.ticket },
      appInfo: appInfo,
      regionId: "cn-shanghai",
      isOverseas: false,
      uiConfig: {
        toolbar: { visible: false },
        rotateDegree: 0,
        hideProgressDialog: true,
        vconsoleVisible: false,
      },
      lyncChannelConfig: [{ lyncChannelName: "lync_adb_shell" }],
      dataChannelConfig: [{ dataChannelName: "wy_vdagent_default_dc" }],
    };

    try {
      state.session = window.Wuying.WebSDK.createSession("appstream", sessionParam);
    } catch (err) {
      setStatus("创建会话失败：" + err.message);
      setBoot("创建会话失败", err.message);
      return;
    }
    state.session.addHandle("onConnected", function () {
      state.connected = true;
      hideBoot();
      setStatus("已连接 · ARM 云手机");
    });
    state.session.addHandle("onDisConnected", function () {
      state.connected = false;
      setStatus("连接已断开，可点「重连」或重新开关机");
    });
    state.session.addConnectionListener("onFirstFrameDecodeSuccess", function () {
      hideBoot();
      state.connected = true;
      setStatus("已连接 · ARM 云手机");
    });
    try {
      state.session.start();
    } catch (err) {
      setStatus("启动会话失败：" + err.message);
      setBoot("启动会话失败", err.message);
    }
  }

  // ---- 启动 ----

  function boot() {
    var powerBtn = $("#powerBtn");
    if (powerBtn) {
      powerBtn.addEventListener("click", function () {
        if (state.power === "on") {
          stopSession();
          postPower("off");
        } else if (state.power !== "creating" && state.power !== "releasing") {
          postPower("on");
        }
      });
    }
    var reconnect = $("#reconnect");
    if (reconnect) {
      reconnect.addEventListener("click", function () {
        stopSession();
        connect();
      });
    }
    var fullscreen = $("#fullscreen");
    if (fullscreen) {
      fullscreen.addEventListener("click", function () {
        var shell = $("#phone");
        if (shell && shell.requestFullscreen) shell.requestFullscreen();
      });
    }
    renderPower();
    fetchPower().then(function () {
      if (state.power === "on") connect();
      else if (state.power === "creating" || state.power === "releasing") pollPower();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
