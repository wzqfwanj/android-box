# 部署手册 —— android-box · 在线真安卓

本应用是**容器里运行的真实 Android**（AOSP Android 11，x86_64），不是界面模拟：
应用机跑 redroid 容器 + Python 标准库 Web 服务，浏览器经 Caddy 看实时画面、直接点按操作、上传安装 APK。
本文所有命令都可直接复制执行。

## 拓扑与事实基线

```
本地部署机（Windows Git Bash）
        │ git archive 打包 + scp（应用机无法访问 GitHub）
        ▼
应用机 dev 180.76.180.249（Ubuntu 24.04 / 内核 6.8 / 2 vCPU / 3.4GB）
        ├── redroid.service      → Docker 容器 redroid（Android 11，x86_64，ADB 仅 127.0.0.1:5555）
        └── android-box.service  → Python 标准库 Web 服务（8150，调用 adb / ffmpeg）
                                                ▲
网关机 aliyun 8.133.209.25（Caddy，站点 https://wkeeli.cn）
        └── handle_path /android/* + flush_interval -1 → http://180.76.180.249:8150
```

| 项目 | 值 |
| --- | --- |
| 应用机 | `ssh dev` → 180.76.180.249，root 登录，Ubuntu 24.04，内核 6.8，2 vCPU / 3.4GB |
| 代码目录 | `/opt/android-box`（普通目录，由部署机 `git archive` 快照同步覆盖，**不是 git 仓库**） |
| 运行用户 | `androidbox`（系统用户，nologin，home 为 `/opt/android-box`） |
| 环境文件 | `/etc/android-box.env`（root:root，0600） |
| 运行数据 | `/var/lib/android-box`（systemd `StateDirectory`：adb 的 HOME 与上传临时目录） |
| 容器 | `redroid`（`--rm`，`--privileged`，ADB 绑定 `127.0.0.1:5555`） |
| 镜像 | `redroid/redroid:11.0.0-latest`（离线导入，应用机无法直连 Docker Hub） |
| 服务名 / 端口 | `android-box` / `0.0.0.0:8150`；`redroid` / 容器内 5555 仅本机映射 |
| 网关机 | `ssh aliyun` → 8.133.209.25，root，Caddy 2.x |
| Caddyfile | `/etc/caddy/Caddyfile`，站点块 `wkeeli.cn { ... }`，子路径 `/android/` |
| 线上地址 | <https://wkeeli.cn/android/>（全站 Basic Auth 保护，服务器本机 127.0.0.1 免认证） |

> 认证约定：应用内部**不实现登录**，全部交给站级 Basic Auth；`/android/` 与 `/phone/`、`/job/` 等子路径行为一致。
> 应用没有任何会话密钥，不需要 `openssl rand`。

## 0 前置条件

- 仓库已在 GitHub 公开：<https://github.com/wzqfwanj/android-box>，部署机 `git push` 可用。
- 部署机（本地 Windows Git Bash）具备 `git`、`ssh`、`scp`、`tar`、`curl`。
- 本地已配置 ssh 别名：`ssh dev`、`ssh aliyun` 可直接 root 登录。
- 应用机：**无需访问 GitHub、无需安装 git**；python3 ≥ 3.10（Ubuntu 24.04 自带 3.12）；
  已 `apt install adb ffmpeg`；Docker 已安装并运行；`8150` 端口与容器映射的 `5555` 未被占用
  （同机的 phone-sim 占用 8140，互不影响）。
- 应用机内核支持 binder（已实测 `modprobe binder_linux` 与 binderfs 挂载可用，见第 2 节）。
- 网关机能出站访问 `180.76.180.249:8150`（安全组/防火墙放行；应用机不要把 5555/8150 暴露到公网）。
- 网关机：Caddy 2.x 运行中，`wkeeli.cn` 证书正常，站点块顶部已有全站 Basic Auth。

部署前先确认环境：

```bash
# 应用机：python3、adb、ffmpeg、docker、端口
ssh dev 'python3 -V; command -v adb ffmpeg docker; docker info >/dev/null && echo docker-ok; ss -lntp | grep -E ":(5555|8150)" || echo "5555/8150 空闲"'
# 网关机：Caddy 与站点配置
ssh aliyun 'caddy version; ls -l /etc/caddy/Caddyfile'
```

## 1 导入 redroid 镜像（离线）

应用机无法直连 Docker Hub，镜像必须离线导入。任选一种方式：

**方式 A：已有导出的镜像 tar（最快）**

```bash
# 本地（或能访问镜像的跳板机）已有 redroid-11.tar
scp redroid-11.tar dev:/tmp/redroid-11.tar
ssh dev 'docker load -i /tmp/redroid-11.tar && rm -f /tmp/redroid-11.tar'
```

**方式 B：用 skopeo 从国内镜像源拉取并转成 docker-archive**

```bash
# 在能上外网的 Linux 机器上操作（Windows 可用 WSL；也可在网关机 aliyun 上操作后 scp 给应用机）
apt install -y skopeo
# 镜像源地址可能变化：按当前可用的 Docker Hub 国内镜像替换 docker.m.daocloud.io
skopeo copy \
  docker://docker.m.daocloud.io/redroid/redroid:11.0.0-latest \
  docker-archive:/tmp/redroid-11.tar:redroid/redroid:11.0.0-latest
scp /tmp/redroid-11.tar dev:/tmp/redroid-11.tar
ssh dev 'docker load -i /tmp/redroid-11.tar && rm -f /tmp/redroid-11.tar'
```

校验：

```bash
ssh dev 'docker images | grep redroid'
ssh dev "docker image inspect redroid/redroid:11.0.0-latest --format '{{.Os}}/{{.Architecture}}'"
# 期望：redroid/redroid  11.0.0-latest  ...  以及 linux/amd64
```

## 2 binder 持久化与校验

redroid 依赖 binder 驱动；`/dev` 是 tmpfs，重启后 binderfs 挂载会消失。推荐做两层持久化：
**模块与参数**用 `/etc/modules-load.d` + `/etc/modprobe.d`，**挂载**由 `redroid.service` 的 `ExecStartPre` 每次启动时确保。

```bash
ssh dev
cat >/etc/modules-load.d/binder_linux.conf <<'EOF'
binder_linux
EOF
cat >/etc/modprobe.d/binder_linux.conf <<'EOF'
options binder_linux devices="binder,hwbinder,vndbinder"
EOF

# 立即加载并挂载（redroid.service 的 ExecStartPre 也会做同样的动作，这里先验证）
modprobe binder_linux
mkdir -p /dev/binderfs
mountpoint -q /dev/binderfs || mount -t binder binder /dev/binderfs
```

校验：

```bash
ls /dev/binderfs                 # 期望看到 binder-control（以及 binder/hwbinder/vndbinder）
mount | grep binderfs            # 期望 binder on /dev/binderfs type binder
lsmod | grep binder              # 期望 binder_linux 已加载
dmesg | tail -20                 # 期望没有 binder 相关报错
```

> 为什么不全写进 `/etc/fstab`：binderfs 的挂载点位于 `/dev`（tmpfs，重启即空），
> 由 `redroid.service` 的 `ExecStartPre`（`mkdir -p` + `mountpoint -q || mount`）负责更省事且可重入；
> 模块与参数则必须持久化，否则重启后容器起不来。

## 3 应用机：建运行用户与目录

```bash
ssh dev

# 系统用户：无 shell、无密码；home 指向代码目录
id androidbox >/dev/null 2>&1 || \
  useradd --system --home-dir /opt/android-box --shell /usr/sbin/nologin --no-create-home androidbox

# 代码目录：普通目录，首次发布时由 deploy.sh 解压覆盖
install -d -o androidbox -g androidbox -m 0755 /opt/android-box

# 运行数据目录：adb 的 HOME（~/.android 授权信息）与 APK 上传临时目录
install -d -o androidbox -g androidbox -m 0750 /var/lib/android-box /var/lib/android-box/tmp
```

> 说明：`/var/lib/android-box` 通常由 `systemd` 的 `StateDirectory=android-box` 创建；这里预先建好
> 是为了让 `/var/lib/android-box/tmp`（`TMPDIR`，见第 5 节）在首次启动前就存在，且权限正确。
> 注意 `androidbox` 用户**不需要** docker 权限：容器由 root 的 `redroid.service` 管理。

## 4 应用机：环境变量文件

```bash
cat >/etc/android-box.env <<'EOF'
# android-box 运行环境（systemd EnvironmentFile，root:root 0600）
HOST=0.0.0.0
PORT=8150
ADB_SERIAL=127.0.0.1:5555
STREAM_W=480
STREAM_FPS=8
STREAM_Q=7
APP_TZ=Asia/Shanghai
EOF
chown root:root /etc/android-box.env
chmod 600 /etc/android-box.env
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8150` | 监听端口，与 Caddyfile 中 `reverse_proxy` 保持一致 |
| `ADB_SERIAL` | `127.0.0.1:5555` | redroid 容器的 ADB 地址 |
| `STREAM_W` | `480` | 推流最大宽度（越小越省 CPU） |
| `STREAM_FPS` | `8` | 推流帧率（2 vCPU 下 8 帧是默认平衡点） |
| `STREAM_Q` | `7` | MJPEG 质量（2~31，越小越清晰、越费 CPU/带宽） |
| `APP_TZ` | `Asia/Shanghai` | 日志/接口时间时区 |

可选：`ADB_BIN` / `FFMPEG_BIN` 指定命令绝对路径（默认按 `PATH` 查找，Ubuntu 下即 `/usr/bin/adb`、`/usr/bin/ffmpeg`）。
`HOME=/var/lib/android-box` 与 `TMPDIR=/var/lib/android-box/tmp` 在 unit 的 `Environment=` 中设置（见第 5 节，原因见 unit 注释）。

## 5 应用机：安装两个单元与首次发布

顺序很重要：**先 redroid（容器起来、ADB 可连），再首次发布 android-box（Web 服务起来）**。

### 5.1 先装 redroid.service

```bash
# 在本地仓库根目录执行
scp deploy/redroid.service dev:/etc/systemd/system/redroid.service
ssh dev 'set -e; chmod 0644 /etc/systemd/system/redroid.service; systemctl daemon-reload; systemctl enable --now redroid'
```

> 整机分辨率（480×854）、DPI（240）、刷新率（15fps）、GPU 模式（guest）、内存（1536m+256m swap）
> 等启动参数**以 `deploy/redroid.service` 的 `ExecStart` 为准**。修改后需重新 scp + `systemctl daemon-reload`
> + `systemctl restart redroid`（会重建容器，已装应用丢失，见第 10 节）。

等待容器启动（首次约 30~90 秒），然后验证：

```bash
ssh dev 'systemctl status redroid --no-pager -l'
ssh dev 'docker ps --filter name=redroid'
ssh dev 'journalctl -u redroid -n 50 --no-pager'
ssh dev 'adb connect 127.0.0.1:5555 && adb devices'
ssh dev 'adb shell getprop sys.boot_completed'      # 返回 1 表示 Android 已启动完成
```

> 手工验证会以 root 身份启动 adb server（占用 5037）。首次启动 Web 服务前执行一次
> `ssh dev 'adb kill-server'`，让服务用户 androidbox 自己拉起 adb server，避免混用。

### 5.2 再装 android-box.service 并首次发布

```bash
# unit 先 scp 上去（此时 /opt/android-box 还是空目录，不能从那里 install）
scp deploy/android-box.service dev:/etc/systemd/system/android-box.service
ssh dev 'set -e; chmod 0644 /etc/systemd/system/android-box.service; systemctl daemon-reload; systemctl enable android-box'

# 首次发布：git push → git archive 打包 → scp → 解压覆盖 → 启动（脚本最后做健康检查）
bash deploy/deploy.sh
```

> 先 `enable` 而不是 `enable --now`：unit 的 `ExecStart` 依赖 `/opt/android-box/app.py`，
> 要等 `deploy/deploy.sh` 把代码同步上去后服务才能启动；脚本会在解压后 `systemctl restart android-box`。

### 5.3 自检

```bash
ssh dev 'systemctl is-active android-box'
ssh dev "curl -s http://127.0.0.1:8150/api/health"
ssh dev "curl -s http://127.0.0.1:8150/api/state"       # 期望 adb_online=true、booted=true
ssh dev "curl -s http://127.0.0.1:8150/api/apps"        # 第三方应用包列表
ssh dev "curl -s --max-time 3 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8150/stream.mjpg"   # 期望 200
ssh dev 'ls -ld /var/lib/android-box /var/lib/android-box/tmp'   # 期望 androidbox:androidbox
```

- `/api/state` 里 `stream.mode` 为 `screenrecord`（实时）或 `screencap`（逐帧兜底），`stream.error` 为空最佳。
- 若 `adb_online=false`：等容器启动完成，或 `journalctl -u android-box -n 100` 看 adb 报错。

## 6 网关机：Caddy 子路径路由

### 6.1 备份配置

```bash
ssh aliyun
cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)
ls -l /etc/caddy/Caddyfile.bak.*
```

### 6.2 插入片段

把本仓库 [`deploy/caddy-android.conf`](./caddy-android.conf) 中「复制以下块」之后的内容，原样粘贴进
`/etc/caddy/Caddyfile` 的 `wkeeli.cn { ... }` 站点块内。

位置要求（Caddy 的 handle 按**书写顺序**匹配）：

- 必须在兜底的 `reverse_proxy localhost:5000` **之前**，否则永远命中不到；
- 建议紧跟已有的 `/phone/` handle_path 块之后。

示意（不是完整文件）：

```caddyfile
wkeeli.cn {
	# ... 既有全局配置（含全站 Basic Auth）...
	# handle_path /phone/* { reverse_proxy http://180.76.180.249:8140 { ... } }

	# ---- android-box 子路径：/android/ ----
	redir /android /android/ 308
	handle_path /android/* {
		reverse_proxy http://180.76.180.249:8150 {
			header_up X-Forwarded-Prefix /android
			flush_interval -1
		}
	}

	# ... 其它 handle ...
	reverse_proxy localhost:5000   # 兜底，必须保持在最后
}
```

关键点：

- 用 `handle_path` 而不是 `handle`：前端用相对路径（`app.js`、`api/state`、`stream.mjpg`），
  `handle_path` 会先剥掉 `/android` 前缀再转发，否则样式/接口 404。
- **`flush_interval -1` 必须有**：它是 MJPEG 实时显示的关键。Caddy 默认缓冲响应，
  不关闭时症状是画面延迟数秒、卡住不动或断开时才出现一次。`/phone/` 这种普通短响应不需要该指令。
- 不要重复写 `basic_auth`：站点块顶部已有全站 Basic Auth，`/android/` 自动覆盖；本机 127.0.0.1 免认证。
- 不要加额外 `header`：站级 CSP 已是 `default-src 'self'`，画面是同源 `<img src="stream.mjpg">`，兼容。
- 不要给这个长连接设置 response timeout / 压缩。

### 6.3 校验并重载

```bash
caddy validate --config /etc/caddy/Caddyfile    # 必须通过再 reload
systemctl reload caddy
systemctl status caddy --no-pager
```

回滚：

```bash
cp -a /etc/caddy/Caddyfile.bak.YYYYMMDDHHMMSS /etc/caddy/Caddyfile
systemctl reload caddy
```

## 7 线上验证（三层）

```bash
# 1) 应用机本机（服务与容器链路）
ssh dev "curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8150/api/health"    # 期望 200
ssh dev "curl -s http://127.0.0.1:8150/api/state"                                     # 期望 adb_online=true、booted=true
ssh dev "curl -s --max-time 3 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8150/stream.mjpg"   # 期望 200

# 2) 网关机本机（--resolve 绕过 DNS、指向本机 Caddy；源 IP 127.0.0.1 免 Basic Auth）
ssh aliyun "curl -sk -o /dev/null -w '%{http_code}\n' --resolve wkeeli.cn:443:127.0.0.1 https://wkeeli.cn/android/"              # 期望 200
ssh aliyun "curl -sk -o /dev/null -w '%{http_code}\n' --resolve wkeeli.cn:443:127.0.0.1 https://wkeeli.cn/android/api/health"   # 期望 200
ssh aliyun "curl -sk -o /dev/null -w '%{http_code}\n' --resolve wkeeli.cn:443:127.0.0.1 https://wkeeli.cn/android/style.css"    # 期望 200（前缀已剥离）
ssh aliyun "curl -sk --max-time 3 -o /dev/null -w '%{http_code}\n' --resolve wkeeli.cn:443:127.0.0.1 https://wkeeli.cn/android/stream.mjpg"   # 期望 200（MJPEG 转发正常）

# 3) 本地 Windows 公网：未登录应被全站登录门拦住
curl -s -o /dev/null -w '%{http_code}\n' https://wkeeli.cn/android/                  # 期望 401（正常现象）
```

> **401 不是故障**：它说明站级 Basic Auth 在 Caddy 层生效。网关机本机用 `--resolve` 探测时源 IP 是
> 127.0.0.1，因此免认证、返回 200；从公网访问必须先登录。

浏览器验证（登录后）：

1. 打开 <https://wkeeli.cn/android/>，输入全站账号 `wkeeli` 与由站点管理员维护的密码。
2. 应看到实时 Android 画面（或短暂「正在连接安卓系统…」后出现桌面）。
3. 点按屏幕 → 设备响应；拖动 → 滑屏；点「返回 / 主屏 / 最近任务 / 音量 / 电源」→ 设备响应。
4. 「安装 APK」选一个 x86_64 兼容的 APK → 状态提示安装结果；返回桌面可见新应用。
5. 再开一个浏览器标签页访问同一地址 → 两个标签页同时看到画面（多端观看）。
6. DevTools → Network：除本站资源与接口外，不应出现任何外部域名请求。

## 8 日志与排错

```bash
# Web 服务（Python 日志、adb/ffmpeg 报错都在这里）
journalctl -u android-box -n 200 --no-pager
journalctl -u android-box -f

# 容器
journalctl -u redroid -n 200 --no-pager
docker logs --tail 200 -f redroid
docker stats --no-stream redroid
docker ps --filter name=redroid

# ADB 链路
adb devices
adb connect 127.0.0.1:5555
adb shell getprop sys.boot_completed
ssh dev "curl -s http://127.0.0.1:8150/api/state"     # 看 adb_online / stream.mode / stream.error

# 网关
journalctl -u caddy -n 200 --no-pager
```

排查思路：页面能开说明 Web 服务正常；黑屏先看 `/api/state` 的 `adb_online` 与 `stream.error`，
再分别查 adb（容器）与 ffmpeg（画面）两条链路。

## 9 升级

发布流程（在本地仓库根目录、Git Bash 中执行）：

```bash
bash deploy/deploy.sh
```

脚本做的事：预检（工作区干净、分支为 main）→ `git push origin main`（失败自动重试 3 次）→
`git archive --format=tar.gz -o .tmp-deploy/... HEAD` → `scp` 到 `dev:/tmp/android-box.tar.gz` →
应用机 `rm -rf /opt/android-box && mkdir -p /opt/android-box` → `tar -xzf` 解压 →
`chown -R androidbox:androidbox` → `chmod -R go-w` → `systemctl restart android-box` →
本机健康检查 → 网关机 `--resolve` 验证 `/android/`、`/android/api/health` →
公网 401 提示。**不重启 redroid 容器、不动 `/var/lib/android-box`（adb 授权数据）。**

等价的纯手工命令（脚本失败时可用于逐步排查）：

```bash
# 1) 本地打包当前提交（HEAD）；未提交的改动不会被打进包
git archive --format=tar.gz -o /tmp/android-box.tar.gz HEAD

# 2) 上传到应用机
scp /tmp/android-box.tar.gz dev:/tmp/android-box.tar.gz

# 3) 应用机解压覆盖 + 权限 + 重启 Web 服务
ssh dev 'set -euo pipefail
rm -rf /opt/android-box
mkdir -p /opt/android-box
tar -xzf /tmp/android-box.tar.gz -C /opt/android-box
chown -R androidbox:androidbox /opt/android-box
chmod -R go-w /opt/android-box
rm -f /tmp/android-box.tar.gz
systemctl restart android-box
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8150/api/health'   # 期望 200
```

要点：

- 只更新 Web 代码时不需要动 redroid 容器，也不需要动 Caddy。
- 若本仓库 `deploy/redroid.service` 或 `deploy/caddy-android.conf` 有变更，按第 5/6 节重新安装：
  redroid 单元变更需要 `systemctl daemon-reload && systemctl restart redroid`（会重建容器，已装 App 丢失）。
- 前端是原生 HTML/CSS/JS，**没有构建步骤**，解压覆盖 + 重启即生效。

## 10 容器重建与数据持久化（可选）

`redroid.service` 的 `docker run` 带 `--rm`：容器停止即删除，因此
`systemctl restart redroid`、机器重启、改启动参数都会得到**全新的 Android**，已安装的 App 全部丢失
（`/var/lib/android-box` 里的 adb 授权数据不受影响）。

如果希望保留已装 App，可给容器加持久化数据目录（可选，默认不启用）：

```bash
# 应用机
install -d -m 0755 /var/lib/redroid-data

# 编辑 /etc/systemd/system/redroid.service 的 ExecStart，在 -v /dev/binderfs:/dev/binderfs 之后追加：
#   -v /var/lib/redroid-data:/data
systemctl daemon-reload
systemctl restart redroid
```

注意：

- 首次挂载空目录时 redroid 会做一次初始化；之后安装的应用、设置都保存在 `/var/lib/redroid-data`。
- 该目录会持续占用磁盘；应用机仅 2 vCPU / 3.4GB，注意 `df -h` 与容器内存。
- 升级镜像版本前先备份该目录（`tar czf`），回退容器版本时数据格式可能不兼容。
- 是否启用属于取舍：默认 `--rm` 更简单、每次都是干净系统，适合演示。

## 11 常见问题

| 现象 | 最可能原因 | 处理 |
| --- | --- | --- |
| 页面能打开但黑屏 / 一直「正在连接安卓系统…」 | adb 未连上，或 ffmpeg 缺失/异常 | `docker ps --filter name=redroid`；`adb devices`；`curl /api/state` 看 `adb_online` 与 `stream.error`；`command -v adb ffmpeg`；`journalctl -u android-box -n 100` |
| 画面显示「逐帧」模式 | redroid 的 `screenrecord` 不可用，后端自动降级 `screencap` | 可继续使用（帧率略低）；`/api/state` 的 `stream.mode/error` 有说明；必要时重启 redroid |
| 点按/滑动没反应 | 设备未 boot 完成、输入注入失败、坐标换算异常 | `adb shell getprop sys.boot_completed` 应为 1；`adb shell input tap 360 640` 手工测试；看页面 toast 与接口 502 报错 |
| 输入中文无效 | 后端 `adb input text` 只支持 ASCII（应用限制） | 点设备里的输入框，用 Android 软键盘输入 |
| 安装 APK 失败 `INSTALL_FAILED_NO_MATCHING_ABIS` | APK 仅支持 ARM，容器是 x86_64 | 换 x86_64 或 universal（含 x86_64）的 APK；本环境装不上 ARM-only 应用 |
| 上传 APK 报 413 | 超过 500MB 上限 | 换小一点的 APK；或修改 app.py 的 `MAX_UPLOAD`（需重新发布） |
| 401 Unauthorized（curl 或浏览器弹登录框） | 全站 Basic Auth 生效 | 正常现象；浏览器输入 `wkeeli` 账号；网关机本机 curl 加 `--resolve` 即免认证 |
| 502 Bad Gateway | android-box 没启动 / 端口不对 / 网关到 8150 不通 | `systemctl status android-box --no-pager -l`；应用机 `curl 127.0.0.1:8150/api/health`；核对 Caddyfile 中 IP:端口 |
| 画面卡住、延迟数秒才刷新 | Caddy 缺 `flush_interval -1`（响应被缓冲） | 在 `reverse_proxy` 里补上该指令，`caddy validate` 后 `systemctl reload caddy` |
| 容器反复重启 / 日志有 OOM | `--memory` 太小或推流参数过高 | `docker stats --no-stream redroid`；调大 `--memory/--memory-swap`，或调小 `STREAM_W` / `STREAM_FPS` 后重启 android-box |
| `docker: command not found` / 容器起不来 | docker 未安装或未运行 | `systemctl status docker`；`systemctl enable --now docker`；`systemctl status redroid` |
| `adb devices` 为空或 `offline` | 容器未 boot 完成 / binderfs 未挂载 / ADB 端口未映射 | `ls /dev/binderfs`；`mount | grep binderfs`；`docker logs redroid`；`adb connect 127.0.0.1:5555` 后重试 |
| 升级后页面还是旧内容 | 浏览器/代理缓存 | 强制刷新（Ctrl+F5） |
| 升级后 502 | 新代码启动失败 | `journalctl -u android-box -n 200 --no-pager`；本地可 `python app.py` 复现 |
| adb 反复掉线、每次都要重新授权 | `/var/lib/android-box`（adb HOME）被删或权限不对 | `ls -ld /var/lib/android-box`；`chown -R androidbox:androidbox /var/lib/android-box`；`systemctl restart android-box` |

---

部署/更新相关文件：

| 文件 | 用途 |
| --- | --- |
| [`deploy/redroid.service`](./redroid.service) | 容器单元：binder 加载/挂载、docker run、内存与端口限制 |
| [`deploy/android-box.service`](./android-box.service) | Web 服务单元：用户、环境文件、StateDirectory、HOME/TMPDIR、加固 |
| [`deploy/caddy-android.conf`](./caddy-android.conf) | 网关 Caddyfile 片段（`/android/` 子路径反代 + `flush_interval -1`） |
| [`deploy/deploy.sh`](./deploy.sh) | 本地一键发布脚本：`git push` → `git archive` 打包 → `scp` → 应用机解压覆盖 → 重启 |
| [`../README.md`](../README.md) | 项目介绍、能力与限制、本地运行、接口与配置 |
