"""支付宝企业支付集成"""
import os
import uuid
from datetime import datetime

from alipay import AliPay


def get_alipay():
    """获取支付宝支付实例（配置从环境变量读取）"""
    app_id = os.environ.get('ALIPAY_APP_ID', '')
    app_private_key_path = os.environ.get('ALIPAY_PRIVATE_KEY_PATH', '')
    alipay_public_key_path = os.environ.get('ALIPAY_PUBLIC_KEY_PATH', '')

    # 支持直接填入密钥字符串（以 -----BEGIN 开头）
    app_private_key_str = os.environ.get('ALIPAY_PRIVATE_KEY', '')
    alipay_public_key_str = os.environ.get('ALIPAY_PUBLIC_KEY', '')

    app_private_key = None
    alipay_public_key = None

    if app_private_key_str and app_private_key_str.startswith('-----BEGIN'):
        app_private_key = app_private_key_str
    elif app_private_key_path and os.path.exists(app_private_key_path):
        with open(app_private_key_path) as f:
            app_private_key = f.read()

    if alipay_public_key_str and alipay_public_key_str.startswith('-----BEGIN'):
        alipay_public_key = alipay_public_key_str
    elif alipay_public_key_path and os.path.exists(alipay_public_key_path):
        with open(alipay_public_key_path) as f:
            alipay_public_key = f.read()

    if not app_id or not app_private_key or not alipay_public_key:
        return None  # 未配置支付宝

    return AliPay(
        appid=app_id,
        app_notify_url=None,  # 在调用时指定
        app_private_key_string=app_private_key,
        alipay_public_key_string=alipay_public_key,
        sign_type='RSA2',
        debug=False
    )


def generate_order_no():
    """生成唯一订单号"""
    now = datetime.now().strftime('%Y%m%d%H%M%S')
    uid = uuid.uuid4().hex[:12]
    return f'ALIPAY{now}{uid}'


def create_trade(order_no, subject, total_amount, notify_url, return_url):
    """
    创建支付宝电脑网站支付交易
    返回: (成功?, 支付页面URL 或 错误信息)
    """
    alipay = get_alipay()
    if not alipay:
        return False, '支付宝未配置，请联系管理员'

    order_string = alipay.api_alipay_trade_page_pay(
        out_trade_no=order_no,
        total_amount=total_amount,
        subject=subject,
        return_url=return_url,
        notify_url=notify_url
    )
    # 沙箱或正式环境
    gateway = 'https://openapi.alipay.com/gateway.do'
    pay_url = f'{gateway}?{order_string}'
    return True, pay_url


def verify_notification(data):
    """
    验证支付宝异步通知签名
    返回: (成功?, 交易信息dict)
    """
    alipay = get_alipay()
    if not alipay:
        return False, {}
    signature = data.pop('sign', '')
    sign_type = data.pop('sign_type', '')
    success = alipay.verify(data, signature)
    return success, data
