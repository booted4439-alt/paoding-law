"""
演算法律服务 - 微信小程序专用 API 路由
提供无状态 token 认证，适配小程序的无 cookie 环境
"""
import os
import uuid
import hmac
import hashlib
import time
import json
import base64
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, request, jsonify, current_app
from models import db, User, SiteSetting, Transaction, RechargeOrder
from models import get_consult_unit_price, get_register_gift_amount

# ---- 无状态 Token（多 worker 兼容） ----
# 使用 HMAC 签名，不依赖内存存储
TOKEN_EXPIRY = 7 * 24 * 3600  # 7天
TOKEN_SECRET = os.environ.get('TOKEN_SECRET') or 'paoding-law-miniapp-secret-2026'


def generate_token(user_id):
    """生成 HMAC 签名 token"""
    expires = int(time.time()) + TOKEN_EXPIRY
    payload = f'{user_id}:{expires}'
    sig = hmac.new(
        TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    return f'{payload}:{sig}'


def verify_token(token):
    """验证 token，返回 user_id 或 None"""
    try:
        parts = token.split(':')
        if len(parts) != 3:
            return None
        user_id, expires_ts, sig = parts
        expires = int(expires_ts)
        if time.time() > expires:
            return None
        expected = f'{user_id}:{expires}'
        check = hmac.new(
            TOKEN_SECRET.encode(),
            expected.encode(),
            hashlib.sha256
        ).hexdigest()[:16]
        if sig != check:
            return None
        return int(user_id)
    except (ValueError, Exception):
        return None


def token_required(f):
    """装饰器：需要有效 token"""
    @wraps(f)
    def wrapper(*a, **kw):
        token = None
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]
        if not token:
            token = request.args.get('token')
        if not token:
            return jsonify({'error': '未登录或登录已过期'}), 401

        user_id = verify_token(token)
        if not user_id:
            return jsonify({'error': '登录已过期，请重新登录'}), 401

        user = db.session.get(User, int(user_id))
        if not user:
            return jsonify({'error': '用户不存在'}), 401

        return f(user, *a, **kw)
    return wrapper


def is_dev_mode():
    """检查是否为开发模式（未配置微信小程序）"""
    appid = os.environ.get('WX_MINI_APPID', '')
    secret = os.environ.get('WX_MINI_SECRET', '')
    return not appid or not secret


# ---- Blueprint ----
mini_app = Blueprint('mini_app', __name__)


# ===================== 微信小程序登录 =====================
@mini_app.route('/api/wechat/login', methods=['POST'])
def wechat_login():
    """
    微信小程序登录
    使用 wx.login() 返回的 code 换取 openid + session_key
    session_key 存储在用户记录中，用于后续 getPhoneNumber 解密
    """
    data = request.get_json() or {}
    code = data.get('code', '').strip()
    nickname = data.get('nickname', '').strip()

    if not code:
        return jsonify({'error': '缺少 code 参数'}), 400

    appid = os.environ.get('WX_MINI_APPID', '')
    secret = os.environ.get('WX_MINI_SECRET', '')

    if not appid or not secret:
        # ---- 开发模式：无微信配置 ----
        code_tag = code[:8]
        uname = nickname or f'mini_dev_{code_tag}'
        user = User.query.filter_by(username=f'mini_dev_{code_tag}').first()
        if not user:
            user = User(
                username=uname,
                email=None,
                phone=None
            )
            user.set_password(code[:16])
            db.session.add(user)
            db.session.commit()
        elif nickname and user.username.startswith('mini_dev_'):
            # 之前用默认名，现在有昵称了就更新
            user.username = nickname
            db.session.commit()

        token = generate_token(user.id)
        return jsonify({
            'token': token,
            'user': serialize_user(user),
            'has_phone': bool(user.phone),
            'dev_mode': True,
            'needs_bind': not bool(user.phone)
        })

    # ---- 生产环境：调用微信接口 ----
    try:
        import requests
        resp = requests.get(
            'https://api.weixin.qq.com/sns/jscode2session',
            params={
                'appid': appid,
                'secret': secret,
                'js_code': code,
                'grant_type': 'authorization_code'
            },
            timeout=10
        )
        result = resp.json()
        if 'openid' not in result:
            current_app.logger.error(f'微信登录失败: {result}')
            return jsonify({'error': '微信登录失败'}), 400

        openid = result['openid']
        session_key = result.get('session_key', '')

        user = User.query.filter_by(openid=openid).first()
        if not user:
            # 新用户：优先使用微信昵称
            uname = nickname if nickname else f'微信用户_{openid[:8]}'
            user = User(
                username=uname,
                email=None,
                openid=openid,
                wx_session_key=session_key,
                phone=None
            )
            user.set_password(openid[:16])
            db.session.add(user)
        else:
            # 老用户：更新 session_key，如果有新昵称则更新
            user.wx_session_key = session_key
            if nickname and (user.username == '新用户' or user.username.startswith('微信用户_') or user.username.startswith('用户_')):
                user.username = nickname
        db.session.commit()

        token = generate_token(user.id)
        return jsonify({
            'token': token,
            'user': serialize_user(user),
            'has_phone': bool(user.phone),
            'dev_mode': False,
            'needs_bind': not bool(user.phone)
        })
    except Exception as e:
        current_app.logger.error(f'微信登录出错: {e}')
        return jsonify({'error': '登录服务异常'}), 500


@mini_app.route('/api/wechat/bind-phone', methods=['POST'])
@token_required
def wechat_bind_phone(current_user):
    """
    绑定手机号到微信账号（支持短信验证码验证）
    模式1：phone + sms_code → 短信验证码验证后绑定
    模式2：encryptedData + iv → 微信 getPhoneNumber 解密绑定
    模式3：phone (dev-only) → 开发环境直接绑定
    可选参数：email → 同时更新邮箱
    """
    from services.sms import check_sms_code

    data = request.get_json() or {}
    encrypted_data = data.get('encryptedData', '')
    iv = data.get('iv', '')
    phone = data.get('phone', '').strip()
    sms_code = data.get('sms_code', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()

    # ---- 模式1 & 3：手动输入手机号 ----
    if phone:
        if not phone.isdigit() or len(phone) < 5:
            return jsonify({'error': '手机号格式不正确'}), 400

        # 模式1：短信验证码验证
        if sms_code:
            verify_result = check_sms_code(phone, sms_code)
            if not (verify_result.get('success') and verify_result.get('verify_result') == 'PASS'):
                return jsonify({'error': '验证码错误或已过期'}), 400
        else:
            return jsonify({'error': '请输入短信验证码'}), 400

        # 设置登录密码（必填，用于网页端密码登录，校验规则同注册页）
        import re
        if not password:
            return jsonify({'error': '请设置登录密码'}), 400
        if len(password) < 6:
            return jsonify({'error': '密码至少6位'}), 400
        if not re.match(r'^[a-zA-Z0-9]+$', password):
            return jsonify({'error': '密码只能包含字母和数字'}), 400

        # 检查重复 → 合并到老账户
        existing = User.query.filter_by(phone=phone).first()
        if existing and existing.id != current_user.id:
            # 🔥 已有用户：合并临时微信账户到老账户
            _merge_wechat_user(current_user, existing, phone, email, password)
            new_token = generate_token(existing.id)
            return jsonify({
                'ok': True,
                'token': new_token,
                'user': serialize_user(existing),
                'needs_bind': False,
                'has_phone': True,
                'merged': True
            })

        current_user.phone = phone
        current_user.set_password(password)

        # 绑定成功后，更新用户名为手机号关联的名字
        if current_user.username in ('新用户',) or current_user.username.startswith('微信用户_') or current_user.username.startswith('mini_'):
            current_user.username = f'用户_{phone[-4:]}'

        # 更新邮箱（选填）
        if email:
            current_user.email = email

        # 首次绑定手机赠送体验金 = 单价×2
        if current_user.balance is None or current_user.balance == 0:
            from models import Transaction
            gift = get_register_gift_amount()
            current_user.balance = gift
            order_no = 'ZS' + datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:8].upper()
            order = RechargeOrder(user_id=current_user.id, order_no=order_no, amount=gift,
                                  payment_method='gift', status='success', admin_id=None,
                                  remark='绑定手机赠送')
            db.session.add(order)
            db.session.flush()
            tx = Transaction(user_id=current_user.id, type='recharge', amount=gift,
                             balance_before=0, balance_after=gift,
                             order_id=order.id, description='赠送')
            db.session.add(tx)

        db.session.commit()
        return jsonify({
            'ok': True,
            'user': serialize_user(current_user),
            'needs_bind': False,
            'has_phone': True
        })

    # ---- 模式2：微信 getPhoneNumber 解密 ----
    if not encrypted_data or not iv:
        return jsonify({'error': '请提供手机号或通过微信获取'}), 400

    appid = os.environ.get('WX_MINI_APPID', '')

    session_key = current_user.wx_session_key
    if not session_key:
        return jsonify({'error': 'session_key 已过期，请重新登录'}), 400

    try:
        phone = decrypt_phone_number(encrypted_data, session_key, iv, appid)
        if not phone:
            return jsonify({'error': '解密失败，手机号无效'}), 400

        existing = User.query.filter_by(phone=phone).first()
        if existing and existing.id != current_user.id:
            # 🔥 已有用户：合并临时微信账户到老账户
            _merge_wechat_user(current_user, existing, phone, email, password)
            new_token = generate_token(existing.id)
            return jsonify({
                'ok': True,
                'token': new_token,
                'user': serialize_user(existing),
                'needs_bind': False,
                'has_phone': True,
                'merged': True
            })

        current_user.phone = phone
        current_user.wx_session_key = None

        # 设置登录密码（选填，用于网页端密码登录）
        if password:
            import re
            if len(password) < 6:
                return jsonify({'error': '密码至少6位'}), 400
            if not re.match(r'^[a-zA-Z0-9]+$', password):
                return jsonify({'error': '密码只能包含字母和数字'}), 400
            current_user.set_password(password)

        # 绑定成功后，更新用户名为手机号关联的名字
        if current_user.username in ('新用户',) or current_user.username.startswith('微信用户_') or current_user.username.startswith('用户_') or current_user.username.startswith('mini_'):
            current_user.username = f'用户_{phone[-4:]}'

        if email:
            current_user.email = email

        # 首次绑定手机赠送体验金 = 单价×2
        if current_user.balance is None or current_user.balance == 0:
            from models import Transaction
            gift = get_register_gift_amount()
            current_user.balance = gift
            order_no = 'ZS' + datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:8].upper()
            order = RechargeOrder(user_id=current_user.id, order_no=order_no, amount=gift,
                                  payment_method='gift', status='success', admin_id=None,
                                  remark='绑定手机赠送')
            db.session.add(order)
            db.session.flush()
            tx = Transaction(user_id=current_user.id, type='recharge', amount=gift,
                             balance_before=0, balance_after=gift,
                             order_id=order.id, description='赠送')
            db.session.add(tx)

        db.session.commit()
        return jsonify({
            'ok': True,
            'user': serialize_user(current_user),
            'needs_bind': False,
            'has_phone': True
        })
    except Exception as e:
        current_app.logger.error(f'绑定手机号解密出错: {e}')
        return jsonify({'error': '手机号获取失败，请重试'}), 500


def _merge_wechat_user(temp_user, existing_user, phone, email='', password=''):
    """
    合并临时微信账户到已有手机账户

    场景：微信一键登录创建了一个临时账户（有 openid 无 phone），
    绑定手机号时发现该手机已注册 → 把临时账户的微信身份转给老账户。

    合并内容：
    - 转移 openid / wx_session_key 到老用户
    - 转移咨询记录、消息、订单、交易等业务数据
    - 删除临时账户
    """
    from models import Consultation, Message, RechargeOrder, Transaction, Invoice, ServiceOrder

    old_id = temp_user.id
    new_id = existing_user.id

    # 先提取临时账户的 openid/session_key，清空后刷新，避免唯一约束冲突
    openid_temp = temp_user.openid
    wx_key_temp = temp_user.wx_session_key
    temp_user.openid = None
    temp_user.wx_session_key = None
    db.session.flush()

    # 1. 转移微信身份到老用户
    if openid_temp and not existing_user.openid:
        existing_user.openid = openid_temp
    if wx_key_temp and not existing_user.wx_session_key:
        existing_user.wx_session_key = wx_key_temp

    # 2. 更新老用户的手机号（其实已经有了，确认一下）
    existing_user.phone = phone

    # 3. 更新邮箱（选填，老用户没有才填）
    if email and not existing_user.email:
        existing_user.email = email

    # 设置登录密码（绑定页填写，用于网页端密码登录）
    if password:
        existing_user.set_password(password)

    # 4. 更新用户名（如果老用户是默认名）
    if existing_user.username in ('新用户',) or existing_user.username.startswith('微信用户_') or existing_user.username.startswith('用户_') or existing_user.username.startswith('mini_'):
        existing_user.username = f'用户_{phone[-4:]}'

    # 5. 转移业务数据
    # 咨询记录：改 user_id
    Consultation.query.filter_by(user_id=old_id).update(
        {Consultation.user_id: new_id}, synchronize_session=False)
    # 消息发送者
    Message.query.filter_by(sender_id=old_id).update(
        {Message.sender_id: new_id}, synchronize_session=False)
    # 充值订单
    RechargeOrder.query.filter_by(user_id=old_id).update(
        {RechargeOrder.user_id: new_id}, synchronize_session=False)
    RechargeOrder.query.filter_by(admin_id=old_id).update(
        {RechargeOrder.admin_id: new_id}, synchronize_session=False)
    # 交易记录
    Transaction.query.filter_by(user_id=old_id).update(
        {Transaction.user_id: new_id}, synchronize_session=False)
    # 发票
    Invoice.query.filter_by(user_id=old_id).update(
        {Invoice.user_id: new_id}, synchronize_session=False)
    # 服务订单
    ServiceOrder.query.filter_by(user_id=old_id).update(
        {ServiceOrder.user_id: new_id}, synchronize_session=False)
    # 律师分配（老用户是律师的情况）
    Consultation.query.filter_by(lawyer_id=old_id).update(
        {Consultation.lawyer_id: new_id}, synchronize_session=False)

    # 6. 删除临时账户
    db.session.delete(temp_user)

    # 7. 如果老用户余额为 0，赠送体验金 = 单价×2
    if not existing_user.balance or existing_user.balance == 0:
        gift = get_register_gift_amount()
        existing_user.balance = gift
        order_no = 'ZS' + datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:8].upper()
        order = RechargeOrder(user_id=existing_user.id, order_no=order_no, amount=gift,
                              payment_method='gift', status='success', admin_id=None,
                              remark='合并账户赠送')
        db.session.add(order)
        db.session.flush()
        tx = Transaction(user_id=existing_user.id, type='recharge', amount=gift,
                         balance_before=0, balance_after=gift,
                         order_id=order.id, description='赠送')
        db.session.add(tx)

    db.session.commit()


def decrypt_phone_number(encrypted_data, session_key, iv, appid):
    """
    微信 getPhoneNumber 数据解密
    参考微信官方文档：https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/signature.html
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    try:
        session_key_bytes = base64.b64decode(session_key)
        encrypted_bytes = base64.b64decode(encrypted_data)
        iv_bytes = base64.b64decode(iv)

        cipher = Cipher(
            algorithms.AES(session_key_bytes),
            modes.CBC(iv_bytes),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        decrypted_bytes = decryptor.update(encrypted_bytes) + decryptor.finalize()

        # 去除 PKCS7 填充
        pad = decrypted_bytes[-1]
        if pad < 1 or pad > 32:
            return None
        decrypted_bytes = decrypted_bytes[:-pad]

        decrypted = json.loads(decrypted_bytes.decode('utf-8'))

        watermark = decrypted.get('watermark', {})
        if watermark.get('appid') != appid:
            current_app.logger.warning(f'watermark appid 不匹配: {watermark.get("appid")} != {appid}')
            return None

        phone_number = decrypted.get('phoneNumber', '')
        if not phone_number:
            return None

        return phone_number
    except Exception as e:
        current_app.logger.error(f'解密手机号异常: {e}')
        return None


# ===================== 账号密码登录（小程序版） =====================
@mini_app.route('/api/miniapp/login', methods=['POST'])
def mini_login():
    """手机号 + 密码登录，返回 token"""
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    password = data.get('password', '').strip()

    if not phone or not password:
        return jsonify({'error': '请输入手机号和密码'}), 400

    user = User.query.filter_by(phone=phone).first()
    if not user:
        return jsonify({'error': '手机号未注册'}), 400

    # 支持验证码直接登录（密码=验证码）
    try:
        from services.sms import check_sms_code
        sms_result = check_sms_code(phone, password)
        if sms_result.get('success') and sms_result.get('verify_result') == 'PASS':
            token = generate_token(user.id)
            return jsonify({
                'token': token,
                'user': serialize_user(user)
            })
    except Exception:
        pass

    # 密码登录
    if not user.check_password(password):
        return jsonify({'error': '密码错误'}), 400

    token = generate_token(user.id)
    return jsonify({
        'token': token,
        'user': serialize_user(user)
    })


# ===================== 注册（小程序版） =====================
@mini_app.route('/api/miniapp/register', methods=['POST'])
def mini_register():
    """注册并直接返回 token"""
    import re
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    phone = data.get('phone', '').strip()
    password = data.get('password', '').strip()
    sms_code = data.get('sms_code', '').strip()
    email = data.get('email', '').strip()

    if not username:
        return jsonify({'error': '请输入用户名'}), 400
    if not phone or not password:
        return jsonify({'error': '请填写手机号和密码'}), 400
    if len(password) < 6:
        return jsonify({'error': '密码至少6位'}), 400
    if not re.match(r'^[a-zA-Z0-9]+$', password):
        return jsonify({'error': '密码只能包含字母和数字'}), 400

    # 验证短信验证码
    if phone and sms_code:
        try:
            from services.sms import check_sms_code
            verify_result = check_sms_code(phone, sms_code)
            if not (verify_result.get('success') and
                    verify_result.get('verify_result') == 'PASS'):
                return jsonify({'error': '验证码错误或已过期'}), 400
        except Exception as e:
            current_app.logger.error(f'验证码核验出错: {e}')
            return jsonify({'error': '验证码核验失败'}), 500
    else:
        return jsonify({'error': '请完成手机验证'}), 400

    # 检查重复
    if User.query.filter_by(phone=phone).first():
        return jsonify({'error': '该手机号已注册'}), 400

    # 创建用户（赠送体验金 = 单价×2）
    gift = get_register_gift_amount()
    user = User(
        username=username,
        email=email or None,
        phone=phone,
        balance=gift
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    order_no = 'ZS' + datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:8].upper()
    order = RechargeOrder(user_id=user.id, order_no=order_no, amount=gift,
                          payment_method='gift', status='success', admin_id=None,
                          remark='注册赠送')
    db.session.add(order)
    db.session.flush()
    tx = Transaction(user_id=user.id, type='recharge', amount=gift,
                     balance_before=0, balance_after=gift,
                     order_id=order.id, description='赠送')
    db.session.add(tx)
    db.session.commit()

    token = generate_token(user.id)
    return jsonify({
        'token': token,
        'user': serialize_user(user)
    }), 201


# ===================== 用户信息 =====================
@mini_app.route('/api/user/profile', methods=['GET'])
@token_required
def user_profile(current_user):
    """获取当前用户信息"""
    return jsonify({
        'user': serialize_user(current_user)
    })


@mini_app.route('/api/user/profile', methods=['PUT'])
@token_required
def update_profile(current_user):
    """更新用户信息"""
    data = request.get_json() or {}
    if 'username' in data and data['username'].strip():
        current_user.username = data['username'].strip()
    if 'email' in data and data['email'].strip():
        current_user.email = data['email'].strip()
    if 'password' in data and data['password']:
        current_user.set_password(data['password'])
    db.session.commit()
    return jsonify({'ok': True, 'user': serialize_user(current_user)})


# ===================== 站点公共信息 =====================
@mini_app.route('/api/site/settings', methods=['GET'])
def site_settings():
    """获取站点公开设置（无需登录）"""
    keys = ['site_name', 'wechat_qr', 'address', 'phone', 'email',
            'icp_beian', 'police_beian']
    result = {}
    for k in keys:
        result[k] = SiteSetting.get(k)
    return jsonify(result)


# ===================== 辅助函数 =====================
def serialize_user(user):
    """序列化用户信息"""
    return {
        'id': user.id,
        'username': user.username,
        'phone': user.phone or '',
        'email': user.email,
        'balance': user.balance or 0,
        'unit_price': int(round(get_consult_unit_price() * 100)),  # 咨询单价（分）
        'is_admin': user.is_admin,
        'is_lawyer': user.is_lawyer
    }
