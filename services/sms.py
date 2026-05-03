"""短信验证码服务 - 子进程方式"""
import json, random, time, subprocess, sys, os

_codes = {}
_SCRIPT = os.path.join(os.path.dirname(__file__), "_send_sms.py")

def generate_code(l=6):
    return "".join(str(random.randint(0, 9)) for _ in range(l))

def send_sms_code(pn, code=None):
    if code is None:
        code = generate_code()
    _codes[pn] = {"code": code, "expire": time.time() + 300}
    try:
        proc = subprocess.run(
            [sys.executable, _SCRIPT, pn],
            capture_output=True, text=True, timeout=25,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            result = json.loads(proc.stdout.strip())
            if result.get("Success") and result.get("Code") == "OK":
                print("[SMS] OK:", pn)
                return {"success": True, "message": "验证码已发送"}
            print("[SMS] 返回:", result.get("Code"), result.get("Message"))
        else:
            print("[SMS] 子进程失败:", proc.stderr[:100])
    except Exception as e:
        print("[SMS] 子进程异常:", str(e)[:100])
    print("[SMS] 本地:", pn, "->", code)
    return {"success": True, "message": "验证码已发送", "debug_code": code}

def check_sms_code(pn, vc):
    r = _codes.get(pn)
    if r and r["code"] == vc and time.time() < r["expire"]:
        _codes.pop(pn, None)
        return {"success": True, "verify_result": "PASS", "message": "验证通过"}
    return {"success": False, "message": "验证码错误"}
