#!/usr/bin/env python3
"""发送短信验证码 - 子进程"""
import json, sys, time, hashlib, hmac, base64, uuid, urllib.parse, os
os.environ["no_proxy"] = "*"
import requests

AK = "ALIYUN_AK_PLACEHOLDER"
SK = "ALIYUN_SK_PLACEHOLDER"
SN = "速通互联验证码"
TC = "100001"

pn = sys.argv[1]
ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
params = {
    "Action": "SendSmsVerifyCode", "Format": "JSON", "Version": "2017-05-25",
    "AccessKeyId": AK, "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
    "SignatureNonce": str(uuid.uuid4()), "Timestamp": ts,
    "PhoneNumber": pn, "SignName": SN, "TemplateCode": TC,
    "TemplateParam": '{"code":"##code##","min":"5"}',
    "OutId": str(uuid.uuid4()) + str(int(time.time() * 1000)),
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
