# windows/

在 Windows 电脑上跑「硬件加速 Android 模拟器 + android-box + SSH 反向隧道」的脚本（部署方案 B）。

## 依赖

| 组件 | 路径 | 说明 |
| --- | --- | --- |
| Android SDK | `C:\Android\Sdk` | `platform-tools`（adb）、`emulator`、系统镜像 `system-images\android-30\default\x86_64` |
| ffmpeg | `C:\Android\tools\ffmpeg\ffmpeg.exe` | 把 scrcpy 的 H.264 视频转成 MJPEG |
| Python 3 | `python` 在 PATH | 需 `Pillow`（`pip install pillow`，兜底截图模式用） |
| AVD | `%USERPROFILE%\.android\avd\wkeeli.avd` | `create-avd.ps1` 生成（480×854 / Android 11 x86_64） |

硬件加速要求：Windows「虚拟机监控程序平台」已启用，`emulator -accel-check` 输出
`WHPX(...) is installed and usable`。

## 用法

```powershell
# 首次：创建 AVD
powershell -ExecutionPolicy Bypass -File .\create-avd.ps1

# 启动：模拟器（headless）+ android-box(127.0.0.1:8150) + SSH 反向隧道（断线重连）
powershell -ExecutionPolicy Bypass -File .\start-android-box.ps1

# 停止
powershell -ExecutionPolicy Bypass -File .\stop-android-box.ps1

# 安装登录自启（写入当前用户 Startup，删除该 vbs 即可取消）
powershell -ExecutionPolicy Bypass -File .\install-startup.ps1
```

日志：`%LOCALAPPDATA%\android-box\logs\`（`app.out.log` / `app.err.log` /
`emulator.out.log` / `emulator.err.log` / `tunnel.log`）。

## 说明

- 隧道方向：本机 `ssh -R 127.0.0.1:8150:127.0.0.1:8150 aliyun`，网关 Caddy 再反代
  `127.0.0.1:8150`（优先）与方案 A 的 `180.76.180.249:8150`（回退）。
- 模拟器参数：`-no-window -no-audio -no-boot-anim -no-snapshot -gpu swiftshader_indirect -accel on`；
  如需真实 GPU 渲染可改成 `-gpu host`（截图回读会变慢，视频流不受影响）。
- 站点仍由网关的全站 Basic Auth 保护，脚本里不保存任何密码（SSH 用本机密钥）。
