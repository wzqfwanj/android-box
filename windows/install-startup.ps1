# 把 android-box 安装为「登录自动启动」（当前用户 Startup 目录，无需管理员）
$ErrorActionPreference = "Stop"
$Repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$StartScript = Join-Path $Repo "windows\start-android-box.ps1"
$Startup = [Environment]::GetFolderPath("Startup")
$Vbs = Join-Path $Startup "android-box.vbs"

$content = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$StartScript""", 0, False
"@
# Windows Script Host 需要 ANSI 或 UTF-16；用 UTF-16 LE 以正确保存中文路径
[System.IO.File]::WriteAllText($Vbs, $content, [System.Text.Encoding]::Unicode)
Write-Output "已安装登录自启：$Vbs"
Write-Output "（删除该文件即可取消自启；手动启动：powershell -ExecutionPolicy Bypass -File `"$StartScript`"）"
