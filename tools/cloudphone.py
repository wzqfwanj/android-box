#!/usr/bin/env python3
"""无影云手机按需管理（纯标准库）：创建 / 释放 / 状态 / 装 APK。

用法：
    python tools/cloudphone.py status
    python tools/cloudphone.py create
    python tools/cloudphone.py create --install-apk ~/Downloads/meituan.apk
    python tools/cloudphone.py install-apk ~/Downloads/meituan.apk
    python tools/cloudphone.py release

设计：平时释放（实例 + 网络都删掉，0 计费）；要用时 create 重建（网络自动建好），
     APK 走 OSS 预签名 URL 让云手机自己下载（本机不需要公网端口）。
凭据：环境变量 ALIYUN_CONFIG（默认 ~/.aliyun/config.json）指向 aliyun CLI 配置。
"""
import argparse
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
NETWORK_NAME = os.environ.get("CP_NETWORK_NAME", "android-box-vpc")
NETWORK_CIDR = os.environ.get("CP_NETWORK_CIDR", "172.16.0.0/16")
IMAGE_ID = os.environ.get("CP_IMAGE_ID", "imgc-0a9mg5540dp7bc0xj")
SPEC = os.environ.get("CP_SPEC", "acp.basic.small")
KEYPAIR_ID = os.environ.get("CP_KEYPAIR_ID", "kp-el9dfrtcfttr7vuun")
OSS_ENDPOINT = "oss-%s.aliyuncs.com" % REGION

ECD_HOST = "ecd.%s.aliyuncs.com" % REGION
AIC_HOST = "eds-aic.%s.aliyuncs.com" % REGION
ECD_VERSION = "2020-09-30"
AIC_VERSION = "2023-09-30"


# ---------------------------------------------------------------- 基础工具

def load_credentials():
    path = os.path.expanduser(os.environ.get("ALIYUN_CONFIG", "~/.aliyun/config.json"))
    cfg = json.load(open(path, encoding="utf-8"))
    profile = cfg["profiles"][0]
    return profile["access_key_id"], profile["access_key_secret"]


AK, SK = load_credentials()


def _percent(value):
    return urllib.parse.quote(str(value), safe="~")


def rpc(host, version, action, params, timeout=30):
    """阿里云 RPC 风格调用（POST + HMAC-SHA1）。"""
    query = {
        "AccessKeyId": AK,
        "Action": action,
        "Format": "JSON",
        "RegionId": REGION,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": version,
    }
    query.update(params)
    canonical = "&".join("%s=%s" % (_percent(k), _percent(query[k])) for k in sorted(query))
    string_to_sign = "POST&%2F&" + _percent(canonical)
    digest = hmac.new((SK + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    body = (canonical + "&Signature=" + _percent(base64.b64encode(digest).decode())).encode()
    req = urllib.request.Request("https://%s/" % host, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("%s 失败 HTTP %s: %s" % (action, exc.code, detail)) from exc


def aic(action, params=None, **kw):
    return rpc(AIC_HOST, AIC_VERSION, action, params or {}, **kw)


def ecd(action, params=None, **kw):
    return rpc(ECD_HOST, ECD_VERSION, action, params or {}, **kw)


def wait(desc, fn, timeout=300, interval=5):
    """轮询 fn() 直到返回真值。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    raise RuntimeError("等待超时：%s（%ss）" % (desc, timeout))


def fmt_status(value):
    return {"RUNNING": "运行中", "CREATING": "创建中", "DELETING": "删除中"}.get(value, value or "-")


# ---------------------------------------------------------------- 网络（办公网络）

def find_network():
    data = ecd("DescribeOfficeSites", {"RegionId": REGION})
    for site in data.get("OfficeSites", []):
        if site.get("Name") == NETWORK_NAME:
            return site
    return None


def ensure_network():
    site = find_network()
    if site:
        print("网络已存在：%s（%s）" % (site["OfficeSiteId"], site.get("Status")))
        return site
    print("创建网络（VPC + 互联网访问）...")
    data = ecd("CreateSimpleOfficeSite", {
        "OfficeSiteName": NETWORK_NAME,
        "VpcType": "standard",
        "CidrBlock": NETWORK_CIDR,
        "EnableInternetAccess": "true",
        "Bandwidth": "10",
        "DesktopAccessType": "Internet",
        "AccountType": "SIMPLE",
    })
    site_id = data["OfficeSiteId"]
    site = wait("网络注册完成", lambda: (lambda s: s if s and s.get("Status") == "REGISTERED" else None)(find_network()),
                timeout=420, interval=10)
    print("网络就绪：%s" % site["OfficeSiteId"])
    return site


def delete_network():
    site = find_network()
    if not site:
        print("网络不存在，跳过")
        return
    site_id = site["OfficeSiteId"]
    print("删除网络 %s ..." % site_id)
    for attempt in range(1, 8):
        try:
            ecd("DeleteOfficeSites", {"OfficeSiteId.1": site_id})
            wait("网络删除完成", lambda: find_network() is None, timeout=300, interval=10)
            print("网络已删除（NAT/EIP 等随之释放）")
            return
        except RuntimeError as exc:
            msg = str(exc)
            if not any(token in msg for token in ("DependencyViolation", "InUse", "NetworkPackage")):
                raise
            # 先把带宽包删掉，等实例组异步清理完成后再重试
            try:
                packages = ecd("DescribeNetworkPackages", {"OfficeSiteId": site_id}).get("NetworkPackages") or []
                for pkg in packages:
                    try:
                        ecd("DeleteNetworkPackages", {"NetworkPackageId": pkg["NetworkPackageId"]})
                        print("  已删除带宽包 %s" % pkg["NetworkPackageId"])
                    except RuntimeError:
                        pass
            except RuntimeError:
                pass
            print("  网络仍有依赖，20 秒后重试（%d/7）..." % attempt)
            time.sleep(20)
    raise RuntimeError("网络删除失败（依赖未释放）：稍后再执行一次 release，或到云手机控制台删除该网络")


def list_groups():
    data = aic("DescribeAndroidInstanceGroups", {"RegionId": REGION})
    return data.get("InstanceGroupModel") or []


def find_group():
    for group in list_groups():
        if group.get("InstanceGroupName") == GROUP_NAME:
            return group
    return None


def group_instances(group_id):
    data = aic("DescribeAndroidInstances", {"RegionId": REGION, "InstanceGroupId": group_id})
    return data.get("InstanceModel") or []


def ensure_group():
    group = find_group()
    if group:
        print("实例组已存在：%s（%s）" % (group["InstanceGroupId"], fmt_status(group.get("InstanceGroupStatus"))))
        return group
    site = ensure_network()
    print("创建实例组（%s / %s / Android 12）..." % (SPEC, IMAGE_ID))
    data = aic("CreateAndroidInstanceGroup", {
        "RegionId": REGION,
        "BizRegionId": REGION,
        "InstanceGroupName": GROUP_NAME,
        "ImageId": IMAGE_ID,
        "InstanceGroupSpec": SPEC,
        "NumberOfInstances": "1",
        "ChargeType": "PostPaid",
        "OfficeSiteId": site["OfficeSiteId"],
        "KeyPairId": KEYPAIR_ID,
    })
    group_id = (data.get("InstanceGroupIds") or [(data.get("InstanceGroupInfos") or [{}])[0].get("InstanceGroupId")])[0]
    print("实例组创建中：%s" % group_id)
    group = wait("实例组运行", lambda: (lambda g: g if g and g.get("InstanceGroupStatus") == "RUNNING" else None)(find_group()),
                 timeout=600, interval=8)
    print("实例组就绪：%s" % group["InstanceGroupId"])
    return group


def release():
    group = find_group()
    if not group:
        print("实例组不存在，跳过")
    else:
        group_id = group["InstanceGroupId"]
        print("删除实例组 %s（实例 %s）..." % (group_id, group.get("InstanceGroupId")))
        aic("DeleteAndroidInstanceGroup", {"InstanceGroupIds.1": group_id})
        wait("实例组删除完成", lambda: find_group() is None, timeout=600, interval=10)
        print("实例组已删除")
    delete_network()
    print("已释放，当前按量计费停止（网络配额也一并删除）。")


# ---------------------------------------------------------------- OSS（临时放 APK）

def oss_headers(method, bucket, key="", content_type="", expires=None):
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    resource = "/%s/%s" % (bucket, key)
    if expires:
        sts = "%s\n\n\n%d\n%s" % (method, expires, resource)
        return None, date, {"OSSAccessKeyId": AK, "Expires": str(expires),
                            "Signature": base64.b64encode(
                                hmac.new(SK.encode(), sts.encode(), hashlib.sha1).digest()).decode()}
    sts = "%s\n\n%s\n%s\n%s" % (method, content_type, date, resource)
    auth = "OSS %s:%s" % (AK, base64.b64encode(
        hmac.new(SK.encode(), sts.encode(), hashlib.sha1).digest()).decode())
    return auth, date, None


def _oss_request(method, bucket, key="", data=b"", content_type=""):
    # urllib 对带 body 的请求会自动补 Content-Type，签名必须用同一个值
    if method in ("PUT", "POST") and not content_type:
        content_type = "application/octet-stream"
    auth, date, _ = oss_headers(method, bucket, key, content_type)
    url = "https://%s.%s/%s" % (bucket, OSS_ENDPOINT, urllib.parse.quote(key))
    req = urllib.request.Request(url, data=data if method in ("PUT", "POST") else None, method=method)
    req.add_header("Date", date)
    req.add_header("Authorization", auth)
    if content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=600) as resp:
        resp.read()


def upload_apk(path):
    apk = os.path.abspath(os.path.expanduser(path))
    size = os.path.getsize(apk)
    md5 = hashlib.md5(open(apk, "rb").read()).hexdigest()
    bucket = "android-box-tmp-%d" % int(time.time())
    print("上传 APK 到临时 OSS（%.1f MB）..." % (size / 1024 / 1024))
    _oss_request("PUT", bucket)
    with open(apk, "rb") as fh:
        _oss_request("PUT", bucket, "meituan.bin", fh.read(), "application/octet-stream")
    expires = int(time.time()) + 3600
    _, _, query = oss_headers("GET", bucket, "meituan.bin", expires=expires)
    url = "https://%s.%s/meituan.bin?%s" % (bucket, OSS_ENDPOINT, urllib.parse.urlencode(query))
    print("临时下载链接已生成（1 小时有效）")
    return {"bucket": bucket, "url": url, "size": size, "md5": md5}


def cleanup_oss(bucket):
    try:
        _oss_request("DELETE", bucket, "meituan.bin")
        _oss_request("DELETE", bucket)
        print("临时 OSS 已清理")
    except Exception as exc:  # noqa: BLE001
        print("提示：临时 OSS 清理失败（可忽略）：%s" % exc)


def run_sync(instance_id, command, wait_ms="3000"):
    """在云手机里同步执行 shell 命令，返回输出文本。"""
    data = aic("RunSyncCommand", {"RegionId": REGION, "InstanceIds.1": instance_id,
                                  "CommandContent": command, "WaitTime": wait_ms})
    rows = data.get("Data") or [{}]
    return rows[0].get("Output", "") or ""


def wait_network(instance_id, timeout=240):
    """等云手机真正能上网（新建网络后 NAT/DNS 需要几十秒）。"""
    deadline = time.time() + timeout
    probe = "curl -s -m 6 -o /dev/null -w '%{http_code}' https://www.aliyun.com"
    while time.time() < deadline:
        try:
            code = run_sync(instance_id, probe).strip()
        except RuntimeError:
            code = ""
        if code in ("200", "301", "302", "403"):
            print("云手机网络已就绪（HTTP %s）" % code)
            return True
        time.sleep(8)
    raise RuntimeError("等待云手机网络就绪超时（%ss）" % timeout)


def current_instance():
    group = find_group()
    if not group:
        return None
    for inst in group_instances(group["InstanceGroupId"]):
        if inst.get("AndroidInstanceStatus") == "RUNNING":
            return inst["AndroidInstanceId"]
    return None


def launch_app(package_name="com.sankuai.meituan", instance_id=None):
    """打开应用：monkey 优先，失败再用 resolve-activity + am start；返回前台校验结果。"""
    instance_id = instance_id or current_instance()
    if not instance_id:
        raise RuntimeError("云手机未开机：先 create")

    def foreground():
        return run_sync(instance_id, "dumpsys activity activities 2>/dev/null | grep -m1 mResumedActivity")

    run_sync(instance_id, "monkey -p %s -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1" % package_name)
    for _ in range(3):
        time.sleep(2)
        out = foreground()
        if package_name in out:
            print(out.strip())
            print("已打开：%s" % package_name)
            return True

    component = run_sync(
        instance_id,
        "cmd package resolve-activity --brief -c android.intent.category.LAUNCHER %s 2>/dev/null | tail -1" % package_name,
    ).strip()
    if "/" in component:
        run_sync(instance_id, "am start -n %s >/dev/null 2>&1" % component)
        for _ in range(3):
            time.sleep(2)
            out = foreground()
            if package_name in out:
                print(out.strip())
                print("已打开：%s" % package_name)
                return True
    raise RuntimeError("已尝试打开 %s，但前台不是它；可稍后再试 launch" % package_name)


# ---------------------------------------------------------------- 装 APK

def install_apk(group_id, apk_path, package_name="com.sankuai.meituan",
                app_name="美团", version="12.69.402", version_code="1200690402"):
    instances = group_instances(group_id)
    if not instances:
        raise RuntimeError("实例组里没有实例")
    instance_id = instances[0]["AndroidInstanceId"]
    wait_network(instance_id)
    oss = upload_apk(apk_path)
    try:
        print("创建应用记录 ...")
        info = {
            "DownloadUrl": oss["url"],
            "Version": version,
            "VersionCode": version_code,
            "PackageName": package_name,
            "ApkSize": str(round(oss["size"] / 1024 / 1024, 1)),
            "Md5": oss["md5"],
        }
        app = aic("CreateApp", {"BizRegionId": REGION, "AppName": app_name,
                                "CustomAppInfo": json.dumps(info, ensure_ascii=False)})
        app_id = app["AppId"]
        print("安装到实例 %s（异步）..." % instance_id)
        task = aic("InstallApp", {"AppIdList.1": str(app_id), "InstanceIdList.1": instance_id})
        task_id = task["TaskId"]
        row = wait("安装任务完成", lambda: (lambda t: t if t and t.get("TaskStatus") in ("Finished", "Failed") else None)(
            (aic("DescribeTasks", {"TaskIds.1": task_id}).get("Data") or [{}])[0]),
            timeout=600, interval=8)
        if row.get("TaskStatus") != "Finished" or int(row.get("FailedChildCount") or 0) > 0:
            raise RuntimeError("安装任务失败：%s（成功 %s / 失败 %s）" % (
                row.get("Param", ""), row.get("SuccessChildCount"), row.get("FailedChildCount")))
        path = run_sync(instance_id, "pm path %s" % package_name)
        if "package:" not in path:
            raise RuntimeError("安装任务显示完成，但设备里没有 %s（可重试 install-apk）" % package_name)
        print("安装完成：%s" % package_name)
        launch_app(package_name, instance_id)
    finally:
        cleanup_oss(oss["bucket"])


# ---------------------------------------------------------------- 状态

def status():
    print("地域：%s" % REGION)
    site = find_network()
    print("网络：%s" % (("%s（%s）" % (site["OfficeSiteId"], site.get("Status"))) if site else "不存在（已释放）"))
    group = find_group()
    if not group:
        print("实例组：不存在（已释放，0 计费）")
        return
    print("实例组：%s  规格 %s  状态 %s" % (group["InstanceGroupId"], group.get("InstanceGroupSpecDescribe"),
                                          fmt_status(group.get("InstanceGroupStatus"))))
    for inst in group_instances(group["InstanceGroupId"]):
        print("  实例 %s  %s  %s" % (inst.get("AndroidInstanceId"), fmt_status(inst.get("AndroidInstanceStatus")),
                                    inst.get("SystemVersion", "")))


def status_json():
    """机器可读状态：供网站「电源」接口使用。"""
    site = find_network()
    group = find_group()
    instance_id = ""
    if group:
        instances = group_instances(group["InstanceGroupId"])
        running = [i for i in instances if i.get("AndroidInstanceStatus") == "RUNNING"]
        instance_id = (running or instances or [{}])[0].get("AndroidInstanceId", "")
    print(json.dumps({
        "network": site["OfficeSiteId"] if site else "",
        "networkStatus": site.get("Status", "") if site else "",
        "groupId": group["InstanceGroupId"] if group else "",
        "groupStatus": group.get("InstanceGroupStatus", "") if group else "",
        "instanceId": instance_id,
    }, ensure_ascii=False))


# ---------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(description="无影云手机按需管理（创建/释放/状态/装APK）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_status = sub.add_parser("status", help="查看网络与实例组状态")
    p_status.add_argument("--json", action="store_true", help="输出 JSON（供程序调用）")
    p_create = sub.add_parser("create", help="创建网络+实例组（按需）")
    p_create.add_argument("--install-apk", help="创建后安装的 APK 路径（如美团）")
    p_install = sub.add_parser("install-apk", help="给现有实例组安装 APK")
    p_install.add_argument("apk", help="APK 文件路径")
    p_launch = sub.add_parser("launch", help="打开云手机里的应用（默认美团）")
    p_launch.add_argument("--package", default="com.sankuai.meituan", help="应用包名（默认美团）")
    sub.add_parser("release", help="释放实例组和网络（停止计费）")
    args = parser.parse_args()

    if args.cmd == "status":
        status_json() if args.json else status()
    elif args.cmd == "create":
        group = ensure_group()
        if args.install_apk:
            install_apk(group["InstanceGroupId"], args.install_apk)
        else:
            print("提示：未安装 APK；需要装美团可用 create --install-apk <路径> 或 install-apk <路径>")
    elif args.cmd == "install-apk":
        group = find_group()
        if not group:
            raise SystemExit("实例组不存在：先运行 create")
        install_apk(group["InstanceGroupId"], args.apk)
    elif args.cmd == "launch":
        launch_app(args.package)
    elif args.cmd == "release":
        release()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print("失败：%s" % exc, file=sys.stderr)
        sys.exit(1)
