#!/usr/bin/env bash
# 给 /android/ 上游加主动健康检查：PC 隧道不可用时自动摘除，流量回到 dev
set -euo pipefail
CF=/etc/caddy/Caddyfile
TS=$(date +%Y%m%d-%H%M%S)
cp "$CF" "/etc/caddy/Caddyfile.bak-health-$TS"
echo "backup: /etc/caddy/Caddyfile.bak-health-$TS"

python3 - "$CF" <<'PY'
import sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()

old = """		reverse_proxy 127.0.0.1:8150 http://180.76.180.249:8150 {
			header_up X-Forwarded-Prefix /android
			lb_policy first
			lb_try_duration 2s
			lb_try_interval 200ms
			flush_interval -1
		}"""

new = """		reverse_proxy 127.0.0.1:8150 http://180.76.180.249:8150 {
			header_up X-Forwarded-Prefix /android
			lb_policy first
			lb_try_duration 2s
			lb_try_interval 200ms
			# PC 隧道断开时主动健康检查会在几秒内把它摘掉，流量自动回到 dev
			health_uri /api/health
			health_interval 4s
			health_timeout 2s
			fail_duration 8s
			flush_interval -1
		}"""

if old not in text:
    if "health_uri /api/health" in text:
        print("已配置健康检查，无需修改")
        raise SystemExit(0)
    raise SystemExit("未找到预期配置块")

open(path, "w", encoding="utf-8").write(text.replace(old, new, 1))
print("patched")
PY

caddy validate --config "$CF" >/dev/null
echo "validate OK"
systemctl reload caddy
sleep 1
systemctl is-active caddy
