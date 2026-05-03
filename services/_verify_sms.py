#!/usr/bin/env python3
"""核验短信验证码 - 子进程"""
import json, sys, time, hashlib, hmac, base64, uuid, urllib.parse, os
os.environ["no_proxy"] = "*"
import requests

AK = os.environ["ALIYUN_SMS_AK"]
SK = os.environ["ALIYUN_SMS_SK"]

pn = sys.argv[1]
vc = sys.argv[2]
ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
params = {
    "Action": "CheckSmsVerifyCode", "Format": "JSON", "Version": "2017-05-25",
    "AccessKeyId": AK, "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
    "SignatureNonce": str(uuid.uuid4()), "Timestamp": ts,
    "PhoneNumber": pn, "VerifyCode": vc,
}
s = sorted(params.items())
c = "&".join(urllib.parse.quote(k, safe='') + "=" + urllib.parse.quote(str(v), safe='') for k, v in s)
ss = "GET&%2F&" + urllib.parse.quote(c, safe='')
sg = base64.b64encode(hmac.new((SK + "&").encode(), ss.encode(), hashlib.sha1).digest()).decode()
params["Signature"] = sg
q = "&".join(urllib.parse.quote(k, safe='') + "=" + urllib.parse.quote(str(v), safe='') for k, v in sorted(params.items()))
try:
    r = requests.get("https://dypnsapi.aliyuncs.com/?" + q, timeout=20)
    print(json.dumps(r.json(), ensure_ascii=False))
except Exception as e:
    print(json.dumps({"Success": False, "Code": "FAIL", "Message": str(e)[:200]}))
    sys.exit(1)
