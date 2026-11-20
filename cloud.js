/* ============================================================
   安卓系统 · ARM 云手机 — 通过阿里云无影 Web SDK 串流
   ------------------------------------------------------------
   后端 /api/cp/ticket 返回连接凭证（Ticket + appInstanceId），
   前端用官方 SDK 在页面内嵌 iframe 里渲染云手机画面，
   触摸/点按由 SDK 直接注入云手机，无需 ADB。
   ============================================================ */

(function () {
  "use strict";

  var $ = function (sel) {
    return document.querySelector(sel);
  };

  var state = { session: null, connected: false, sdkRetry: 0 };

  function setStatus(text) {
    var el = $("#status");
    if (el) el.textContent = text;
  }

  function setBootNote(text) {
    var el = $("#bootNote");
    if (el) el.textContent = text;
  }

  function hideBoot() {
    var boot = $("#boot");
    if (boot && !boot.classList.contains("is-hidden")) boot.classList.add("is-hidden");
  }

  function api(path) {
    return fetch(path, { cache: "no-store" }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
        return data;
      });
    });
  }

  function sdkReady() {
    return window.Wuying && window.Wuying.WebSDK && typeof window.Wuying.WebSDK.createSession === "function";
  }

  function connect() {
    if (!sdkReady()) {
      if (state.sdkRetry++ < 40) {
        setTimeout(connect, 250);
      } else {
        setStatus("SDK 加载失败，请刷新页面");
        setBootNote("SDK 加载失败");
      }
      return;
    }
    setStatus("获取连接凭证…");
    setBootNote("获取连接凭证 — 云端串流");
    api("../api/cp/ticket")
      .then(function (data) {
        if (!data.ok || !data.ticket) {
          setStatus("云手机暂不可用：" + (data.error || "没有 Ticket"));
          setBootNote(data.error || "云手机暂不可用");
          return;
        }
        startSession(data);
      })
      .catch(function (err) {
        setStatus("获取凭证失败：" + err.message);
        setBootNote("获取凭证失败");
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
      setBootNote("创建会话失败");
      return;
    }
    state.session.addHandle("onConnected", function () {
      state.connected = true;
      setStatus("已连接 · ARM 云手机");
      hideBoot();
    });
    state.session.addHandle("onDisConnected", function () {
      state.connected = false;
      setStatus("连接已断开，点击「重连」");
    });
    state.session.addConnectionListener("onFirstFrameDecodeSuccess", function () {
      hideBoot();
      setStatus("已连接 · ARM 云手机");
    });
    try {
      state.session.start();
    } catch (err) {
      setStatus("启动会话失败：" + err.message);
      setBootNote("启动会话失败");
    }
    setTimeout(function () {
      if (!state.connected) setBootNote("云端串流连接中…（首次连接可能要几秒）");
    }, 3000);
  }

  function boot() {
    var reconnect = $("#reconnect");
    if (reconnect) {
      reconnect.addEventListener("click", function () {
        window.location.reload();
      });
    }
    var fullscreen = $("#fullscreen");
    if (fullscreen) {
      fullscreen.addEventListener("click", function () {
        var shell = $("#phone");
        if (shell && shell.requestFullscreen) shell.requestFullscreen();
      });
    }
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
