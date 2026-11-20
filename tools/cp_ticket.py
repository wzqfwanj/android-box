#!/usr/bin/env python3
"""获取无影云手机连接 Ticket（仅标准库，无第三方依赖）。

用法:
    python cp_ticket.py              # 按 CP_GROUP_NAME（默认 android-box-vpc）自动找运行中的实例
    python cp_ticket.py <instance-id># 指定实例

输出: JSON {ticket, instanceId, appInstanceId, appInstanceGroupId, ...}
凭据来源：环境变量 ALIYUN_CONFIG（默认 ~/.aliyun/config.json）。
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
GROUP_NAME = os.environ.get("CP_GROUP_NAME", "android-box-vpc")
ECD_HOST = "ecd.%s.aliyuncs.com" % REGION
AIC_HOST = "eds-aic.%s.aliyuncs.com" % REGION
AIC_VERSION = "2023-09-30"


def _percent(value):
    return urllib.parse.quote(str(value), safe="~")


def _credentials():
    path = os.path.expanduser(os.environ.get("ALIYUN_CONFIG", "~/.aliyun/config.json"))
    if not os.path.exists(path):
        return None, "缺少阿里云凭据文件：%s" % path
    try:
        profile = json.load(open(path, encoding="utf-8"))["profiles"][0]
        return (profile["access_key_id"], profile["access_key_secret"]), ""
    except Exception as exc:  # noqa: BLE001
        return None, "阿里云凭据格式错误：%s" % exc


def _rpc(host, version, action, params):
    creds, err = _credentials()
    if creds is None:
        raise RuntimeError(err)
    ak, sk = creds
    query = {
        "AccessKeyId": ak, "Action": action, "Format": "JSON", "RegionId": REGION,
        "SignatureMethod": "HMAC-SHA1", "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": version,
    }
    query.update(params)
    canonical = "&".join("%s=%s" % (_percent(k), _percent(query[k])) for k in sorted(query))
    digest = hmac.new((sk + "&").encode(), ("POST&%2F&" + _percent(canonical)).encode(),
                      hashlib.sha1).digest()
    body = (canonical + "&Signature=" + _percent(base64.b64encode(digest).decode())).encode()
    req = urllib.request.Request("https://%s/" % host, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        hint = "（RAM 用户缺少 eds-aic 权限）" if "Forbidden" in detail else ""
        raise RuntimeError("HTTP %s%s: %s" % (exc.code, hint, detail)) from exc


def resolve_instance():
    """按实例组名找当前运行中的实例；不存在时返回 (None, 友好提示)。"""
    data = _rpc(AIC_HOST, AIC_VERSION, "DescribeAndroidInstanceGroups",
                {"RegionId": REGION, "InstanceGroupName": GROUP_NAME})
    groups = [g for g in (data.get("InstanceGroupModel") or [])
              if g.get("InstanceGroupName") == GROUP_NAME]
    if not groups:
        return None, "云手机当前未创建（已释放）。需要用的时候运行：python tools/cloudphone.py create"
    group = groups[0]
    if group.get("InstanceGroupStatus") != "RUNNING":
        return None, "云手机正在创建/变更中，请稍后刷新"
    instances = _rpc(AIC_HOST, AIC_VERSION, "DescribeAndroidInstances",
                     {"RegionId": REGION, "InstanceGroupId": group["InstanceGroupId"]})
    running = [i for i in (instances.get("InstanceModel") or [])
               if i.get("AndroidInstanceStatus") == "RUNNING"]
    if not running:
        return None, "云手机实例还没就绪，请稍后刷新"
    return running[0]["AndroidInstanceId"], ""


def get_ticket(instance=""):
    creds, err = _credentials()
    if creds is None:
        return {"error": err}
    instance_id = instance
    if not instance_id:
        instance_id, err = resolve_instance()
        if not instance_id:
            return {"error": err}
    try:
        data = _rpc(AIC_HOST, AIC_VERSION, "BatchGetAcpConnectionTicket",
                    {"RegionId": REGION, "InstanceIds.1": instance_id})
    except RuntimeError as exc:
        return {"error": str(exc)}
    models = data.get("InstanceConnectionModels") or []
    if not models:
        return {"error": "没有返回连接凭证", "raw": data}
    m = models[0]
    if not m.get("Ticket"):
        return {"error": m.get("ErrorCode") or "没有 Ticket", "raw": m}
    return {
        "ticket": m.get("Ticket", ""),
        "instanceId": instance_id,
        "taskStatus": m.get("TaskStatus", ""),
        "appInstanceId": m.get("AppInstanceId", ""),
        "appInstanceGroupId": m.get("AppInstanceGroupId", ""),
        "persistentAppInstanceId": m.get("PersistentAppInstanceId", ""),
        "port": m.get("Port"),
        "errorCode": m.get("ErrorCode", ""),
    }


def main():
    instance = sys.argv[1] if len(sys.argv) > 1 else ""
    result = get_ticket(instance)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ticket") else 1


if __name__ == "__main__":
    sys.exit(main())
