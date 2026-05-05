"""短信验证码服务 - 子进程方式（含开发模式本地兜底）"""
import json, time, subprocess, sys, os, random

_SCRIPT_SEND = os.path.join(os.path.dirname(__file__), "_send_sms.py")
_SCRIPT_VERIFY = os.path.join(os.path.dirname(__file__), "_verify_sms.py")

def generate_code(l=6):
    """生成随机验证码"""
    return "".join(str(random.randint(0, 9)) for _ in range(l))


# 开发模式的本地验证码存储器
_dev_codes = {}


def _is_dev():
    """判断是否为开发模式（动态检查环境变量，避免模块缓存问题）"""
    return not bool(os.environ.get('ALIYUN_SMS_AK'))

_last_sent = {}

def send_sms_code(pn, code=None):
    now = time.time()
    if pn in _last_sent and now - _last_sent[pn] < 55:
        print("[SMS] 本地频率限制，跳过:", pn)
        return {"success": True, "message": "验证码已发送，请查收短信"}
    _last_sent[pn] = now

    if _is_dev():
        # 开发模式：生成验证码存内存，不调阿里云
        code = code or generate_code()
        _dev_codes[pn] = code
        print(f"[SMS-DEV] 发送验证码 {code} 到 {pn}")
        return {"success": True, "message": f"验证码已发送（开发模式: {code}）"}

    # 生产模式：调阿里云
    try:
        proc = subprocess.run(
            [sys.executable, _SCRIPT_SEND, pn],
            capture_output=True, text=True, timeout=25,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            result = json.loads(proc.stdout.strip())
            code = result.get("Code")
            if result.get("Success") and code == "OK":
                print("[SMS] OK:", pn)
                return {"success": True, "message": "验证码已发送"}
            if code == "biz.FREQUENCY":
                print("[SMS] 频率限制，使用已有验证码:", pn)
                return {"success": True, "message": "验证码已发送，请查收短信"}
            print("[SMS] 返回:", code, result.get("Message"))
        else:
            print("[SMS] 子进程失败:", proc.stderr[:100])
    except Exception as e:
        print("[SMS] 子进程异常:", str(e)[:100])
    return {"success": False, "message": "短信发送失败，请稍后重试"}

def check_sms_code(pn, vc):
    if _is_dev():
        # 开发模式：从内存中取验证码校验
        stored = _dev_codes.get(pn)
        if stored and stored == vc:
            print(f"[SMS-DEV] 验证通过: {pn}")
            _dev_codes.pop(pn, None)
            return {"success": True, "verify_result": "PASS", "message": "验证通过"}
        print(f"[SMS-DEV] 验证失败: {pn}, 期望={stored}, 传入={vc}")
        return {"success": False, "message": "验证码错误"}

    # 生产模式：调阿里云核验
    try:
        proc = subprocess.run(
            [sys.executable, _SCRIPT_VERIFY, pn, vc],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            result = json.loads(proc.stdout.strip())
            if result.get("Success") and result.get("Code") == "OK":
                print("[SMS] 验证通过:", pn)
                return {"success": True, "verify_result": "PASS", "message": "验证通过"}
            print("[SMS] 验证失败:", result.get("Code"), result.get("Message"))
    except Exception as e:
        print("[SMS] 验证异常:", str(e)[:100])
    return {"success": False, "message": "验证码错误"}
