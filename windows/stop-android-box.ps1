# 停止 android-box（模拟器 + Web 服务 + SSH 隧道）
$ErrorActionPreference = "Continue"
$Adb = "C:\Android\Sdk\platform-tools\adb.exe"

Write-Output "停止 android-box（python）..."
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'android-box[\\/]+app\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "  killed python $($_.ProcessId)" }

Write-Output "停止 SSH 反向隧道..."
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
  Where-Object { $_.CommandLine -match '127\.0\.0\.1:8150:127\.0\.0\.1:8150' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "  killed ssh $($_.ProcessId)" }

if (Test-Path $Adb) {
  Write-Output "关闭模拟器..."
  & $Adb emu kill 2>&1 | Out-Null
}
Write-Output "已停止。"
