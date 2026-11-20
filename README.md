# android-box · 在线真安卓（ARM 云手机）

网页里直接操作一台真正的 Android 设备：**阿里云无影云手机**（Android 12 / arm64），
可以安装运行只有 ARM 库的 App（例如美团）。画面、触摸、键盘全部走阿里云官方 Web SDK
在浏览器里串流，服务端只负责发页面和换连接凭证（Ticket），不转发视频。

- 线上地址：<https://wkeeli.cn/android/>（全站 Basic Auth 保护）
- 实现：Python 标准库单文件后端（`app.py`）+ 一个前端页面 + 官方 Web SDK 的 iframe 容器

## 架构

```
浏览器 ──(页面/静态资源/Ticket)──> 网关机 Caddy ──> app.py (127.0.0.1:8150)
   │                                                  │
   └──(WebRTC 串流 + 触摸注入，直连)──> 阿里云无影串流网关 ──> 云手机
                                   app.py 用 AK 调 eds-aic OpenAPI 换 Ticket
```

## 目录结构

```text
android-box/
├── app.py                  # 单文件后端：静态页 + /api/health + /api/cp/ticket + cp/ SDK 静态资源
├── index.html              # 页面（iframe 里是云手机画面）
├── cloud.js                # 前端：取 Ticket → 创建 SDK 会话 → 状态提示/重连/全屏
├── style.css               # 灰阶样式（5 个 CSS 变量，375px 无横向滚动）
├── favicon.svg
├── tools/cp_ticket.py      # 纯标准库：HMAC-SHA1 签名调 BatchGetAcpConnectionTicket
├── cp/                     # 无影 Web SDK（不入库，部署时手动放置，见下）
│   ├── WuyingWebSDK.js
│   └── sdk/ASP/container.html
├── deploy/
│   ├── deploy.sh           # 一键发布到网关机（git archive → scp → systemd restart）
│   └── android-box.service # systemd 单元
└── tests/test_app.py       # 单元测试（不依赖真实云手机）
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 页面 |
| GET | `/cloud/` | 308 跳到 `/`（兼容旧链接，保留 Caddy 前缀） |
| GET | `/api/health` | 存活探针：`{"ok":true,"mode":"cloud-phone",...}` |
| GET | `/api/cp/ticket` | 调阿里云换 Ticket；未配置实例 503，凭据/接口错误 502 |
| GET | `/cp/*`、`/sdk/*` | Web SDK 静态资源（仅 `cp/` 目录内，防目录穿越） |

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址（只给 Caddy 反代用，不要直接暴露公网） |
| `PORT` | `8150` | 监听端口 |
| `CLOUD_PHONE_INSTANCE` | 空 | 云手机实例 ID（`acp-...`）；不配置则页面提示未配置 |
| `CP_REGION` | `cn-shanghai` | 云手机地域 |
| `ALIYUN_CONFIG` | `~/.aliyun/config.json` | 阿里云凭据文件（取第一个 profile 的 AK/SK） |

## 获取 Web SDK（不入库）

阿里云官方离线包，仅授权账号本人使用，因此不随公开仓库分发：

1. 打开 <https://help.aliyun.com/zh/ecp/get-cloud-phone-sdk-and-demo>，下载 **Web/H5 SDK**（`WebHtmlSDK.zip`）
2. 解压后把这两个文件放到本目录：

```text
cp/WuyingWebSDK.js
cp/sdk/ASP/container.html
```

## 本地运行

```bash
python3 app.py
# 浏览器打开 http://127.0.0.1:8150
```

配置了 `CLOUD_PHONE_INSTANCE` 且本机有阿里云凭据时，页面就是云手机；
否则显示「未配置云手机实例」。

```bash
CLOUD_PHONE_INSTANCE=acp-xxxxxxxx python3 app.py
# 或指定凭据文件
ALIYUN_CONFIG=/etc/android-box/aliyun.json CLOUD_PHONE_INSTANCE=acp-xxxxxxxx python3 app.py
```

## 部署（阿里云网关机常驻）

一次性准备（网关机）：

```bash
# 1) 服务配置（root 600）
mkdir -p /etc/android-box
cat > /etc/android-box/cloud.env <<'EOF'
CLOUD_PHONE_INSTANCE=acp-xxxxxxxx
CP_REGION=cn-shanghai
ALIYUN_CONFIG=/etc/android-box/aliyun.json
EOF
chmod 600 /etc/android-box/cloud.env

# 2) 阿里云 AK（RAM 用户需 eds-aic 相关权限；建议只给云手机所需权限）
cat > /etc/android-box/aliyun.json <<'EOF'
{"profiles":[{"name":"default","mode":"AK","access_key_id":"...","access_key_secret":"..."}]}
EOF
chmod 600 /etc/android-box/aliyun.json

# 3) Caddy：/android/* 反代本机 8150（一次性配置，deploy.sh 不修改 Caddy）
#    handle_path /android/* { reverse_proxy 127.0.0.1:8150 { header_up X-Forwarded-Prefix /android ... } }
```

之后每次发布：

```bash
bash deploy/deploy.sh
```

脚本会推 GitHub、`git archive` → scp → 解压到 `/opt/android-box`、同步 systemd 单元、
重启服务，并验证 `/api/health`、`/android/`、`/android/api/cp/ticket`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 计费与释放

云手机按量约 **0.30 元/小时**（4 小时一个计费单位，关机也计费），不用时务必释放实例组
（`DeleteAndroidInstanceGroup`），否则会持续扣费；长期使用可以改为包月（更便宜）。

## 限制

- 页面只做「看画面 + 点按/滑动/输入」，没有自建 APK 上传入口；装 App 用云手机控制台，
  或后续用 `CreateApp/InstallApp` API 扩展。
- 串流依赖阿里云官方 Web SDK 与 `g.alicdn.com` 静态资源；网关已为 `/android/*` 单独放宽 CSP
  与同源 iframe（其他站点的安全头不受影响）。
- 全站 Basic Auth 由 Caddy 负责，本服务不内置登录。

## 许可证

MIT（本仓库代码）。`cp/` 下的无影 Web SDK 版权归阿里云，仅按官方授权范围内使用。
