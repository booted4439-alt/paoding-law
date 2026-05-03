"""阿里云短信验证码服务"""
import json
import random
from alibabacloud_dypnsapi20170525.client import Client as DypnsapiClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_dypnsapi20170525 import models as dypnsapi_models
from alibabacloud_tea_util import models as util_models

# 阿里云配置
ACCESS_KEY_ID = "ALIYUN_AK_PLACEHOLDER"
ACCESS_KEY_SECRET = "ALIYUN_SK_PLACEHOLDER"
SIGN_NAME = "速通互联验证码"
TEMPLATE_CODE = "100001"  # 验证码模板


def create_client():
    """创建阿里云客户端"""
    config = open_api_models.Config(
        access_key_id=ACCESS_KEY_ID,
        access_key_secret=ACCESS_KEY_SECRET,
    )
    config.endpoint = "dypnsapi.aliyuncs.com"
    return DypnsapiClient(config)


def generate_code(length=6):
    """生成随机验证码"""
    return ''.join(str(random.randint(0, 9)) for _ in range(length))


def send_sms_code(phone_number: str, code: str = None) -> dict:
    """
    发送短信验证码
    返回: {"success": bool, "message": str, "biz_id": str, "verify_code": str}
    """
    if code is None:
        code = generate_code()

    client = create_client()
    request = dypnsapi_models.SendSmsVerifyCodeRequest(
        phone_number=phone_number,
        sign_name=SIGN_NAME,
        template_code=TEMPLATE_CODE,
        template_param='{"code":"##code##","min":"5"}',
    )
    runtime = util_models.RuntimeOptions()

    try:
        resp = client.send_sms_verify_code_with_options(request, runtime)
        body = resp.body
        if body.success:
            model = body.model
            return {
                "success": True,
                "message": body.message or '发送成功',
                "biz_id": model.biz_id if hasattr(model, 'biz_id') else '',
                "verify_code": model.verify_code if hasattr(model, 'verify_code') else '',
                "out_id": model.out_id if hasattr(model, 'out_id') else '',
            }
        else:
            return {"success": False, "message": body.message or '发送失败', "code": body.code}
    except Exception as e:
        msg = str(e)
        recommend = ""
        try:
            recommend = e.data.get("Recommend", "")
        except Exception:
            pass
        return {"success": False, "message": msg, "recommend": recommend}


def check_sms_code(phone_number: str, verify_code: str) -> dict:
    """
    核验短信验证码
    返回: {"success": bool, "message": str, "verify_result": str}
    """
    client = create_client()
    request = dypnsapi_models.CheckSmsVerifyCodeRequest(
        phone_number=phone_number,
        verify_code=verify_code,
    )
    runtime = util_models.RuntimeOptions()

    try:
        resp = client.check_sms_verify_code_with_options(request, runtime)
        body = resp.body
        if body.success:
            result = body.model.verify_result  # "PASS" or "REJECT"
            return {
                "success": True,
                "verify_result": result,
                "message": body.message,
            }
        else:
            return {"success": False, "message": body.message, "code": body.code}
    except Exception as e:
        msg = str(e)
        recommend = ""
        try:
            recommend = e.data.get("Recommend", "")
        except Exception:
            pass
        return {"success": False, "message": msg, "recommend": recommend}
