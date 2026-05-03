"""短信验证码服务 - 子进程方式"""
import json, time, subprocess, sys, os

_SCRIPT_SEND = os.path.join(os.path.dirname(__file__), "_send_sms.py")
_SCRIPT_VERIFY = os.path.join(os.path.dirname(__file__), "_verify_sms.py")

def generate_code(l=6):
    """生成随机验证码（兼容旧引用）"""
    import random
    return "".join(str(random.randint(0, 9)) for _ in range(l))


def send_sms_code(pn, code=None):
    try:
        proc = subprocess.run(
            [sys.executable, _SCRIPT_SEND, pn],
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
    return {"success": False, "message": "短信发送失败，请稍后重试"}

def check_sms_code(pn, vc):
    # 通过阿里云API核验（不受多进程影响）
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
