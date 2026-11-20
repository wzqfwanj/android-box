#!/usr/bin/env python3
"""获取无影云手机连接 Ticket（仅标准库，无第三方依赖）。

用法: python cp_ticket.py <instance-id>
输出: JSON {ticket, appInstanceId, appInstanceGroupId, persistentAppInstanceId, port}

凭据来源：环境变量 ALIYUN_CONFIG（默认 ~/.aliyun/config.json）指向的 aliyun CLI 配置
（取第一个 profile 的 AK/SK），不把密钥写进仓库。
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

REGION = os.environ.get("CP_REGION", "cn-shanghai")
HOST = "eds-aic.%s.aliyuncs.com" % REGION
API_VERSION = "2023-09-30"


def _percent(value):
    return urllib.parse.quote(str(value), safe="~")


def _load_credentials():
    path = os.path.expanduser(os.environ.get("ALIYUN_CONFIG", "~/.aliyun/config.json"))
    if not os.path.exists(path):
        return None, "缺少阿里云凭据文件：%s" % path
    try:
        cfg = json.load(open(path, encoding="utf-8"))
        profile = cfg["profiles"][0]
        return (profile["access_key_id"], profile["access_key_secret"]), ""
    except Exception as exc:  # noqa: BLE001
        return None, "阿里云凭据格式错误：%s" % exc


def _sign(params, secret):
    canonical = "&".join("%s=%s" % (_percent(k), _percent(params[k])) for k in sorted(params))
    string_to_sign = "POST&%2F&" + _percent(canonical)
    digest = hmac.new((secret + "&").encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return canonical + "&Signature=" + _percent(base64.b64encode(digest).decode("utf-8"))


def get_ticket(instance):
    creds, err = _load_credentials()
    if creds is None:
        return {"error": err}
    ak, secret = creds
    params = {
        "AccessKeyId": ak,
        "Action": "BatchGetAcpConnectionTicket",
        "Format": "JSON",
        "RegionId": REGION,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": API_VERSION,
        "InstanceIds.1": instance,
    }
    body = _sign(params, secret).encode("utf-8")
    req = urllib.request.Request("https://%s/" % HOST, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        hint = "（RAM 用户缺少 eds-aic 权限）" if "Forbidden" in detail else ""
        return {"error": "HTTP %s%s: %s" % (exc.code, hint, detail)}
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    models = data.get("InstanceConnectionModels") or []
    if not models:
        return {"error": "没有返回连接凭证", "raw": data}
    m = models[0]
    if not m.get("Ticket"):
        return {"error": m.get("ErrorCode") or "没有 Ticket", "raw": m}
    return {
        "ticket": m.get("Ticket", ""),
        "taskStatus": m.get("TaskStatus", ""),
        "appInstanceId": m.get("AppInstanceId", ""),
        "appInstanceGroupId": m.get("AppInstanceGroupId", ""),
        "persistentAppInstanceId": m.get("PersistentAppInstanceId", ""),
        "port": m.get("Port"),
        "errorCode": m.get("ErrorCode", ""),
    }


def main():
    instance = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CLOUD_PHONE_INSTANCE", "")
    if not instance:
        print(json.dumps({"error": "缺少实例 ID"}, ensure_ascii=False))
        return 1
    result = get_ticket(instance)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ticket") else 1


if __name__ == "__main__":
    sys.exit(main())
