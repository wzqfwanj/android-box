# 在 Windows 上运行 android-box（方案 2：本机硬件加速模拟器 + SSH 反向隧道）
#
# 用法：powershell -ExecutionPolicy Bypass -File start-android-box.ps1
# 作用：
#   1) 若模拟器未启动，headless 启动 AVD wkeeli（WHPX 硬件加速）
#   2) 等 adb 连上、Android boot 完成
#   3) 启动 android-box（scrcpy 视频流 + 低延迟注入，监听 127.0.0.1:8150）
#   4) 建立到 wkeeli.cn 跳板机的 SSH 反向隧道（远端 127.0.0.1:8150 → 本机 8150），断线自动重连
# 日志：%LOCALAPPDATA%\android-box\logs

$ErrorActionPreference = "Continue"
$Sdk = "C:\Android\Sdk"
$Adb = Join-Path $Sdk "platform-tools\adb.exe"
$Emulator = Join-Path $Sdk "emulator\emulator.exe"
$Ffmpeg = "C:\Android\tools\ffmpeg\ffmpeg.exe"
$Repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$AppDir = $Repo
$LogDir = Join-Path $env:LOCALAPPDATA "android-box\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("android-box-" + (Get-Date -Format "yyyyMMdd") + ".log")

function Log([string]$msg) {
  $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
  Write-Output $line
  Add-Content -Path $Log -Value $line
}

Log "=== start-android-box ==="

# 1) 启动模拟器（如果没在跑）
$devices = & $Adb devices 2>&1
if ($devices -notmatch "emulator-\d+\s+device") {
  Log "模拟器未运行，启动 AVD wkeeli ..."
  $env:ANDROID_SDK_ROOT = $Sdk
  $env:ANDROID_HOME = $Sdk
  Start-Process -FilePath $Emulator -ArgumentList @(
    "-avd", "wkeeli",
    "-no-window", "-no-audio", "-no-boot-anim",
    "-no-snapshot", "-gpu", "swiftshader_indirect",
    "-accel", "on"
  ) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "emulator.out.log") -RedirectStandardError (Join-Path $LogDir "emulator.err.log") | Out-Null
} else {
  Log "模拟器已在运行"
}

# 2) 等 adb 设备 + boot 完成
Log "等待 Android 启动完成 ..."
& $Adb start-server 2>&1 | Out-Null
$booted = $false
for ($i = 0; $i -lt 180; $i++) {
  $state = (& $Adb devices 2>&1) -join "`n"
  if ($state -match "emulator-\d+\s+device") {
    $boot = (& $Adb shell getprop sys.boot_completed 2>&1) -join ""
    if ($boot.Trim() -eq "1") { $booted = $true; break }
  }
  Start-Sleep -Seconds 2
}
if (-not $booted) {
  Log "错误：等待 Android 启动超时，查看 $LogDir\emulator.err.log"
  exit 1
}
Log "Android 已启动"

# 3) 启动 android-box（若未运行）
$portBusy = $false
try { $portBusy = (Test-NetConnection -ComputerName 127.0.0.1 -Port 8150 -InformationLevel Quiet -WarningAction SilentlyContinue) } catch {}
if (-not $portBusy) {
  Log "启动 android-box ..."
  $env:ADB_BIN = $Adb
  $env:FFMPEG_BIN = $Ffmpeg
  $env:ADB_SERIAL = "emulator-5554"
  $env:HOST = "127.0.0.1"
  $env:PORT = "8150"
  $env:SCRCPY_VIDEO = "1"
$env:CLOUD_PHONE_INSTANCE = "acp-3ur525pcli6l70smy"
$env:CP_REGION = "cn-shanghai"
  $env:JPEG_QUALITY = "68"
  $env:PYTHONUNBUFFERED = "1"
  Start-Process -FilePath "python" -ArgumentList @((Join-Path $AppDir "app.py")) -WorkingDirectory $AppDir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "app.out.log") -RedirectStandardError (Join-Path $LogDir "app.err.log") | Out-Null
  Start-Sleep -Seconds 4
} else {
  Log "android-box 已在运行"
}

# 4) SSH 反向隧道（断线自动重连）
Log "启动 SSH 反向隧道：aliyun:127.0.0.1:8150 -> 本机:8150"
while ($true) {
  & ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 `
    -R 127.0.0.1:8150:127.0.0.1:8150 aliyun 2>>(Join-Path $LogDir "tunnel.log")
  Log "隧道断开，5 秒后重连..."
  Start-Sleep -Seconds 5
}
