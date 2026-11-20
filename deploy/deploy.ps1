#!/usr/bin/env pwsh
# =============================================================================
# deploy/deploy.ps1 —— android-box 一键发布（Windows PowerShell 版）
#
# 与 deploy.sh 等价：推 GitHub → git archive → scp 到网关机 → 解压覆盖
# /opt/android-box → 同步 systemd 单元 → 重启 → 健康检查 → 网关机验证。
#
# 前置：~/.ssh/config 配好 Host aliyun；网关机已有 /etc/android-box/cloud.env、
#       /etc/android-box/aliyun.json、Caddy 反代 /android/* → 127.0.0.1:8150。
#
# 用法（仓库根目录）：pwsh deploy/deploy.ps1
# =============================================================================
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repo
$appHost = if ($env:APP_HOST) { $env:APP_HOST } else { "aliyun" }
$branch = if ($env:BRANCH) { $env:BRANCH } else { "main" }
$siteUrl = "https://wkeeli.cn/android/"

function Fail($msg) { Write-Host "[deploy] 失败：$msg" -ForegroundColor Red; exit 1 }
function Log($msg) { Write-Host "[deploy] $msg" }

if (-not (Test-Path "app.py")) { Fail "仓库根找不到 app.py" }
foreach ($cmd in @("git", "ssh", "scp", "curl")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { Fail "缺少命令：$cmd" }
}
if (git status --porcelain) { git status --short; Fail "工作区不干净：请先提交再发布" }
$current = git rev-parse --abbrev-ref HEAD
if ($current -ne $branch) { Fail "当前分支 $current ≠ $branch" }

Log "推送 $branch 到 GitHub ..."
$pushed = $false
for ($i = 1; $i -le 3; $i++) {
    git push origin $branch
    if ($LASTEXITCODE -eq 0) { $pushed = $true; break }
    Log "推送失败（$i/3），3 秒后重试 ..."
    Start-Sleep -Seconds 3
}
if (-not $pushed) { Fail "git push 连续失败：检查 GitHub 凭据/代理" }
$commit = (git rev-parse --short HEAD)

$tmp = Join-Path $repo ".tmp-deploy"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$tgz = Join-Path $tmp "android-box-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()).tar.gz"
try {
    Log "打包 HEAD ..."
    git archive --format=tar.gz -o $tgz HEAD
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $tgz)) { Fail "git archive 失败" }

    Log "上传到网关机（$appHost）..."
    scp -q $tgz "${appHost}:/tmp/android-box.tar.gz"
    if ($LASTEXITCODE -ne 0) { Fail "scp 上传失败" }

    Log "解压覆盖 /opt/android-box 并重启服务 ..."
    $remote = @'
set -euo pipefail
rm -rf /opt/android-box
mkdir -p /opt/android-box
tar -xzf /tmp/android-box.tar.gz -C /opt/android-box
rm -f /tmp/android-box.tar.gz
chmod -R go-w /opt/android-box
test -f /etc/android-box/cloud.env || { echo "缺少 /etc/android-box/cloud.env" >&2; exit 1; }
if ! cmp -s /opt/android-box/deploy/android-box.service /etc/systemd/system/android-box.service; then
  install -m 0644 /opt/android-box/deploy/android-box.service /etc/systemd/system/android-box.service
  systemctl daemon-reload
fi
systemctl enable android-box >/dev/null 2>&1 || true
systemctl restart android-box
'@
    $remote | ssh $appHost "bash -s"
    if ($LASTEXITCODE -ne 0) { Fail "网关机更新失败。排查：ssh $appHost 'journalctl -u android-box -n 100 --no-pager'" }

    Log "健康检查 ..."
    $code = ""
    for ($i = 0; $i -lt 20; $i++) {
        $code = (ssh $appHost "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8150/api/health").Trim()
        if ($code -eq "200") { break }
        Start-Sleep -Seconds 1
    }
    if ($code -ne "200") { Fail "健康检查失败（实际：$code）" }
    Log "/api/health -> 200"

    $pageCode = (ssh $appHost "curl -sk -o /dev/null -w '%{http_code}' --resolve wkeeli.cn:443:127.0.0.1 '$siteUrl'").Trim()
    if ($pageCode -ne "200") { Fail "网关机探测 $siteUrl 未返回 200（实际：$pageCode）" }
    Log "/android/ -> 200"

    $ticketCode = (ssh $appHost "curl -sk -o /dev/null -w '%{http_code}' --resolve wkeeli.cn:443:127.0.0.1 '${siteUrl}api/cp/ticket'").Trim()
    if ($ticketCode -ne "200") { Fail "/android/api/cp/ticket 未返回 200（实际：$ticketCode）" }
    Log "/android/api/cp/ticket -> 200"

    try {
        $pub = (curl.exe -s -o NUL -m 15 -w "%{http_code}" $siteUrl).Trim()
        if ($pub -eq "401") { Log "公网未登录 -> 401（Basic Auth 生效）" }
        else { Log "提示：公网未登录返回 $pub（预期 401）" }
    } catch { Log "提示：公网探测失败（不影响发布）" }

    Log "发布完成，commit $commit"
    Write-Host "`n线上地址：$siteUrl"
}
finally {
    if (Test-Path $tgz) { Remove-Item $tgz -Force -ErrorAction SilentlyContinue }
    if ((Get-ChildItem $tmp -Force -ErrorAction SilentlyContinue | Measure-Object).Count -eq 0) {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}
