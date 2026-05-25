"""邮件发送服务 - SMTP（异步发送）"""
import smtplib, os, threading
from email.mime.text import MIMEText
from email.header import Header

SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465
SMTP_USER = os.environ.get("SMTP_USER", "paodinglaw@163.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "PLuYDdwBa8rYeiMD")


def _send(to_addr, subject, body):
    """实际发送（同步，不对外暴露）"""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = SMTP_USER
        msg["To"] = Header(to_addr)
        msg["Subject"] = Header(subject, "utf-8")
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [to_addr], msg.as_string())
    except Exception as e:
        print(f"[MAIL] 发送失败: {e}")


def send_email(to_addr, subject, body):
    """异步发送邮件，立即返回"""
    threading.Thread(target=_send, args=(to_addr, subject, body), daemon=True).start()
    return True, "已加入发送队列"


def notify_new_message(consultation, message, sender, contact_email):
    subject = f"新咨询消息 - {consultation.title or '法律咨询'}"
    body = (
        f"用户 {sender.username}（{sender.phone or '未绑定手机'}）"
        f"在咨询「{consultation.title or '法律咨询'}」中发送了一条新消息。\n\n"
        f"消息内容：\n{message.content or '(文件消息)'}\n\n"
        f"查看详情：https://calculuslaw.com/admin/consultations/{consultation.id}"
    )
    return send_email(contact_email, subject, body)
