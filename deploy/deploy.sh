#!/usr/bin/env bash
# =============================================================================
# deploy/deploy.sh —— android-box 一键发布脚本（本地执行）
#
# 部署模型（应用机无法访问 GitHub，因此不使用 git clone / git pull）：
#   本地仓库 --git archive--> tar.gz --scp--> 应用机 /tmp --解压覆盖--> /opt/android-box --restart--> Web 服务
#   /opt/android-box 是普通目录（不是 git 仓库）；应用机不需要安装 git，也不需要访问 GitHub。
#   只覆盖 Web 代码目录，不动 /var/lib/android-box（adb 数据）与 /etc/android-box.env（配置），
#   也不重启 redroid 容器。
#
# 适用环境：本地 Windows（Git Bash）、Linux、macOS；需要 git、ssh、scp、tar、curl。
#
# 做什么：
#   1) 预检：仓库根目录有 app.py；命令 git/ssh/scp/tar/curl 存在；工作区干净；当前分支 = BRANCH（默认 main）
#   2) 把当前分支推到 GitHub（远端备份；失败自动重试 3 次，仍失败则中止）
#   3) git archive 打包 HEAD 为 tar.gz（临时文件放 .tmp-deploy/，脚本结束由 trap 清理）
#   4) scp 上传到应用机 /tmp/android-box.tar.gz
#   5) ssh dev 应用机（root）：清空并重建 /opt/android-box → 解压 → chown androidbox → 去掉组/其他写位
#      → systemctl restart android-box
#   6) 健康检查轮询 http://127.0.0.1:8150/api/health 期望 200（最多 20 次 × 1s）
#   7) ssh aliyun 网关机用 --resolve 验证 /android/ 与 /android/api/health 期望 200
#   8) 公网 https://wkeeli.cn/android/ 期望 401（全站 Basic Auth 生效，非阻断提示）
#
# 前置条件：
#   - ~/.ssh/config 已配置 Host dev → 180.76.180.249、Host aliyun → 8.133.209.25，可直接 root 登录
#   - 应用机已完成一次性系统部署（androidbox 用户、/etc/android-box.env、两个 systemd 单元、redroid 容器），
#     见 deploy/DEPLOY.md
#   - 在仓库根目录执行：bash deploy/deploy.sh
#
# 注意：本脚本不修改 systemd 单元与 Caddy 配置——它们是一次性配置；若仓库中的
#       deploy/redroid.service、deploy/android-box.service 或 deploy/caddy-android.conf 有变更，
#       请按 DEPLOY.md 第 5/6 节手工同步（改 redroid 单元会重建容器，已装 App 会丢失）。
# =============================================================================

set -euo pipefail

# 定位仓库根目录（脚本位于 <repo>/deploy/deploy.sh）
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

APP_HOST="${APP_HOST:-dev}"          # 应用机 ssh 别名
GW_HOST="${GW_HOST:-aliyun}"         # 网关机 ssh 别名
BRANCH="${BRANCH:-main}"             # 发布分支
APP_HEALTH="http://127.0.0.1:8150/api/health"
SITE_URL="https://wkeeli.cn/android/"
GW_RESOLVE="--resolve wkeeli.cn:443:127.0.0.1"   # 网关机本机探测：绕过 DNS，源 IP 127.0.0.1 免认证

TMP_DIR="$REPO_DIR/.tmp-deploy"
TMP_TGZ="$TMP_DIR/android-box-$(date +%s).tar.gz"
cleanup() {
  rm -f "$TMP_TGZ"
  rmdir "$TMP_DIR" 2>/dev/null || true
}
trap cleanup EXIT

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] 失败：%s\n' "$*" >&2; exit 1; }

# ---------- 1. 本地预检 ----------
log "仓库：$REPO_DIR"
[ -f app.py ] || die "仓库根目录找不到 app.py，请在 android-box 仓库根目录执行：bash deploy/deploy.sh"

for cmd in git ssh scp tar curl; do
  command -v "$cmd" >/dev/null 2>&1 || die "缺少命令：$cmd（请安装后重试）"
done

if [ -n "$(git status --porcelain)" ]; then
  git status --short
  die "Git 工作区不干净（上方为未提交改动）。脚本只发布 HEAD 快照，请先提交或 stash 再发布。"
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$current_branch" = "$BRANCH" ] || \
  die "当前分支为 $current_branch，不是 $BRANCH；若要发布其它分支：BRANCH=$current_branch bash deploy/deploy.sh"

# ---------- 2. 推送到 GitHub（失败重试 3 次） ----------
log "推送 $BRANCH 到 GitHub ..."
pushed=""
for attempt in 1 2 3; do
  if git push origin "$BRANCH"; then
    pushed="1"
    break
  fi
  if [ "$attempt" -lt 3 ]; then
    log "推送失败（第 $attempt/3 次），5 秒后重试 ..."
    sleep 5
  fi
done
[ "$pushed" = "1" ] || \
  die "git push 连续 3 次失败：请检查 GitHub 凭据与网络/代理。不要跳过推送，先修复后再发布。"
COMMIT="$(git rev-parse --short HEAD)"
log "已推送 commit $COMMIT"

# ---------- 3. 打包 HEAD 快照 ----------
mkdir -p "$TMP_DIR"
log "打包 HEAD 为 tar.gz（git archive）..."
git archive --format=tar.gz -o "$TMP_TGZ" HEAD || \
  die "git archive 失败：确认 HEAD 有效（git log -1）"
[ -s "$TMP_TGZ" ] || die "打包产物为空：$TMP_TGZ"
log "包大小：$(wc -c < "$TMP_TGZ" | tr -d ' ') 字节"

# ---------- 4. 上传到应用机 ----------
log "上传到应用机（$APP_HOST:/tmp/android-box.tar.gz）..."
scp -q "$TMP_TGZ" "$APP_HOST:/tmp/android-box.tar.gz" || \
  die "scp 上传失败：确认 ssh $APP_HOST 可登录、应用机 /tmp 可写且空间充足"

# ---------- 5. 应用机解压覆盖并重启 Web 服务（只动代码目录） ----------
log "应用机解压覆盖 /opt/android-box 并重启 android-box ..."
ssh "$APP_HOST" 'set -euo pipefail
rm -rf /opt/android-box
mkdir -p /opt/android-box
tar -xzf /tmp/android-box.tar.gz -C /opt/android-box
chown -R androidbox:androidbox /opt/android-box
chmod -R go-w /opt/android-box
rm -f /tmp/android-box.tar.gz
systemctl restart android-box' || \
  die "应用机更新失败。排查：ssh $APP_HOST 'journalctl -u android-box -n 100 --no-pager; systemctl status android-box --no-pager -l'"

# ---------- 6. 应用机健康检查（最多 20 秒） ----------
log "等待应用机健康检查（$APP_HEALTH，最多 20 秒）..."
app_code=""
for _ in {1..20}; do
  app_code="$(ssh "$APP_HOST" "curl -s -o /dev/null -w '%{http_code}' '$APP_HEALTH'" || true)"
  [ "$app_code" = "200" ] && break
  sleep 1
done
[ "$app_code" = "200" ] || \
  die "应用机健康检查未返回 200（实际：${app_code:-无响应}）。排查：ssh $APP_HOST 'systemctl status android-box --no-pager -l; journalctl -u android-box -n 100 --no-pager'"
log "应用机 /api/health -> 200"

# 附加信息：adb 是否在线（不阻断，失败只提示；adbd 未就绪时 /api/state 可能较慢，限制 10s）
state_json="$(ssh "$APP_HOST" "curl -s --max-time 10 http://127.0.0.1:8150/api/state" 2>/dev/null || true)"
case "$state_json" in
  *'"adb_online": true'*|*'"adb_online":true'*) log "应用机 adb 在线（redroid 已连上）" ;;
  *) log "提示：adb 可能未连上，可稍后用 curl /api/state 确认（不影响 Web 服务本身）" ;;
esac

# ---------- 7. 网关机验证 ----------
log "验证网关机（$GW_HOST）：$SITE_URL ..."

site_code="$(ssh "$GW_HOST" "curl -sk -o /dev/null -w '%{http_code}' $GW_RESOLVE '$SITE_URL'" || true)"
[ "$site_code" = "200" ] || \
  die "网关机探测 $SITE_URL 未返回 200（实际：${site_code:-无响应}）。排查：ssh $GW_HOST 'caddy validate --config /etc/caddy/Caddyfile; journalctl -u caddy -n 100 --no-pager'"
log "/android/ -> 200"

health_code="$(ssh "$GW_HOST" "curl -sk -o /dev/null -w '%{http_code}' $GW_RESOLVE '${SITE_URL}api/health'" || true)"
[ "$health_code" = "200" ] || \
  die "网关机探测 ${SITE_URL}api/health 未返回 200（实际：${health_code:-无响应}）。若 /android/ 正常而 API 异常，通常是 Caddy 前缀未剥（应使用 handle_path）"
log "/android/api/health -> 200"

# ---------- 8. 公网登录门提示（非阻断） ----------
pub_code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$SITE_URL" || true)"
if [ "$pub_code" = "401" ]; then
  log "公网未登录访问 -> 401（全站 Basic Auth 生效，符合预期）"
else
  log "提示：公网未登录访问返回 ${pub_code:-无响应}；预期 401。若为 200 说明站点登录门配置可能变化，请人工确认。"
fi

# ---------- 9. 完成 ----------
log "发布完成，commit $COMMIT"
printf '\n线上地址：%s\n需全站 Basic Auth 登录（账号 wkeeli，密码由站点管理员维护）。\n' "$SITE_URL"
