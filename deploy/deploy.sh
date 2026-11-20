#!/usr/bin/env bash
# =============================================================================
# deploy/deploy.sh —— android-box 一键发布（本地执行 → 阿里云网关常驻）
#
# 架构：浏览器 ⇄ 阿里云无影串流网关 ⇄ 云手机；本服务只在本机换 Ticket + 发静态页，
#       由网关 Caddy 反向代理到 https://wkeeli.cn/android/。
#
# 做什么：
#   1) 预检：仓库根有 app.py；git/ssh/scp/tar/curl 存在；工作区干净；分支 = BRANCH
#   2) git push 到 GitHub（远端备份，失败重试 3 次）
#   3) git archive 打包 HEAD → scp 到网关机 /tmp → 解压覆盖 /opt/android-box
#   4) 若本机有 cp/（无影 Web SDK，不入库）→ 一起打包上传解压，避免部署把 SDK 删掉
#   5) 同步 deploy/android-box.service 到 systemd（改了才 reload）→ restart
#   6) 健康检查 /api/health、/android/、/android/api/cp/ticket、/cp/WuyingWebSDK.js
#   7) 公网未登录访问 /android/ 期望 401（全站 Basic Auth 生效，非阻断）
#
# 前置条件（一次性）：
#   - ~/.ssh/config 配 Host aliyun → 网关机 root 可登录
#   - 网关机已有 /etc/android-box/cloud.env、/etc/android-box/aliyun.json
#   - 网关 Caddy 的 /android/* 反代到 127.0.0.1:8150（一次性配置，本脚本不修改 Caddy）
#   - 本机 cp/ 内有无影 Web SDK（见 README「获取 Web SDK」）
#
# 在仓库根执行：bash deploy/deploy.sh
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

APP_HOST="${APP_HOST:-aliyun}"
BRANCH="${BRANCH:-main}"
SITE_URL="https://wkeeli.cn/android/"
GW_RESOLVE="--resolve wkeeli.cn:443:127.0.0.1"

TMP_DIR="$REPO_DIR/.tmp-deploy"
TMP_TGZ="$TMP_DIR/android-box-$(date +%s).tar.gz"
CP_TGZ="$TMP_DIR/android-box-cp.tar.gz"
cleanup() { rm -f "$TMP_TGZ" "$CP_TGZ"; rmdir "$TMP_DIR" 2>/dev/null || true; }
trap cleanup EXIT

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] 失败：%s\n' "$*" >&2; exit 1; }

# ---------- 1. 预检 ----------
log "仓库：$REPO_DIR"
[ -f app.py ] || die "仓库根找不到 app.py"
for cmd in git ssh scp tar curl; do
  command -v "$cmd" >/dev/null 2>&1 || die "缺少命令：$cmd"
done
if [ -n "$(git status --porcelain)" ]; then
  git status --short
  die "工作区不干净：请先提交再发布"
fi
current_branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$current_branch" = "$BRANCH" ] || die "当前分支 $current_branch ≠ $BRANCH"

# ---------- 2. 推送 GitHub ----------
log "推送 $BRANCH 到 GitHub ..."
pushed=""
for attempt in 1 2 3; do
  if git push origin "$BRANCH"; then pushed="1"; break; fi
  [ "$attempt" -lt 3 ] && { log "推送失败（$attempt/3），5 秒后重试 ..."; sleep 5; }
done
[ "$pushed" = "1" ] || die "git push 连续失败：检查 GitHub 凭据/代理"
COMMIT="$(git rev-parse --short HEAD)"

# ---------- 3. 打包上传 ----------
mkdir -p "$TMP_DIR"
git archive --format=tar.gz -o "$TMP_TGZ" HEAD
scp -q "$TMP_TGZ" "$APP_HOST:/tmp/android-box.tar.gz" || die "scp 上传失败"

# 无影 Web SDK（cp/）不入库，单独打包上传，避免覆盖部署时丢文件
if [ -f "$REPO_DIR/cp/WuyingWebSDK.js" ]; then
  log "打包 Web SDK（cp/，不入库）..."
  tar -czf "$CP_TGZ" -C "$REPO_DIR" cp
  scp -q "$CP_TGZ" "$APP_HOST:/tmp/android-box-cp.tar.gz" || die "SDK 上传失败"
else
  log "警告：本机没有 cp/WuyingWebSDK.js，部署后页面会因缺 SDK 无法串流（见 README）"
fi

# ---------- 4. 解压 + 同步 systemd 单元 + 重启 ----------
log "更新网关机 /opt/android-box 并重启服务 ..."
ssh "$APP_HOST" 'set -euo pipefail
rm -rf /opt/android-box
mkdir -p /opt/android-box
tar -xzf /tmp/android-box.tar.gz -C /opt/android-box
rm -f /tmp/android-box.tar.gz
if [ -f /tmp/android-box-cp.tar.gz ]; then
  tar -xzf /tmp/android-box-cp.tar.gz -C /opt/android-box
  rm -f /tmp/android-box-cp.tar.gz
fi
chmod -R go-w /opt/android-box
test -f /etc/android-box/cloud.env || { echo "缺少 /etc/android-box/cloud.env" >&2; exit 1; }
if ! cmp -s /opt/android-box/deploy/android-box.service /etc/systemd/system/android-box.service; then
  install -m 0644 /opt/android-box/deploy/android-box.service /etc/systemd/system/android-box.service
  systemctl daemon-reload
fi
systemctl enable android-box >/dev/null 2>&1 || true
systemctl restart android-box' || die "网关机更新失败。排查：ssh $APP_HOST 'journalctl -u android-box -n 100 --no-pager'"

# ---------- 5. 健康检查 ----------
code=""
for _ in {1..20}; do
  code="$(ssh "$APP_HOST" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8150/api/health" || true)"
  [ "$code" = "200" ] && break
  sleep 1
done
[ "$code" = "200" ] || die "健康检查失败（实际：${code:-无响应}）"
log "/api/health -> 200"

# ---------- 6. 网关机验证 ----------
page_code="$(ssh "$APP_HOST" "curl -sk -o /dev/null -w '%{http_code}' $GW_RESOLVE '$SITE_URL'" || true)"
[ "$page_code" = "200" ] || die "网关机探测 $SITE_URL 未返回 200（实际：${page_code:-无响应}）"
log "/android/ -> 200"

ticket_code="$(ssh "$APP_HOST" "curl -sk -o /dev/null -w '%{http_code}' $GW_RESOLVE '${SITE_URL}api/cp/ticket'" || true)"
if [ "$ticket_code" = "200" ]; then
  log "/android/api/cp/ticket -> 200（云手机运行中）"
elif [ "$ticket_code" = "503" ]; then
  log "/android/api/cp/ticket -> 503（云手机已释放，正常；需要时 python tools/cloudphone.py create）"
else
  die "/android/api/cp/ticket 异常（实际：${ticket_code:-无响应}）"
fi

sdk_code="$(ssh "$APP_HOST" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8150/cp/WuyingWebSDK.js" || true)"
[ "$sdk_code" = "200" ] || die "SDK 静态资源缺失（/cp/WuyingWebSDK.js = ${sdk_code:-无响应}）：按 README 放好 cp/ 后重发"
log "/cp/WuyingWebSDK.js -> 200"

# ---------- 7. 公网登录门提示 ----------
pub_code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$SITE_URL" || true)"
if [ "$pub_code" = "401" ]; then
  log "公网未登录 -> 401（Basic Auth 生效，符合预期）"
else
  log "提示：公网未登录返回 ${pub_code:-无响应}（预期 401，请人工确认登录门）"
fi

log "发布完成，commit $COMMIT"
printf '\n线上地址：%s\n' "$SITE_URL"
