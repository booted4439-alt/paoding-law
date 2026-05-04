"""
庖丁法律服务 - 微信小程序专用 API 路由
提供无状态 token 认证，适配小程序的无 cookie 环境
"""
import os
import uuid
import hmac
import hashlib
import time
import json
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, request, jsonify, current_app
from models import db, User, SiteSetting

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


# ---- Blueprint ----
mini_app = Blueprint('mini_app', __name__)


# ===================== 微信小程序登录 =====================
@mini_app.route('/api/wechat/login', methods=['POST'])
def wechat_login():
    """
    微信小程序登录
    使用 wx.login() 返回的 code 换取 openid
    注意：需要配置微信小程序的 appid 和 secret
    """
    data = request.get_json() or {}
    code = data.get('code', '').strip()

    if not code:
        return jsonify({'error': '缺少 code 参数'}), 400

    # 从环境变量读取微信小程序配置
    appid = os.environ.get('WX_MINI_APPID', '')
    secret = os.environ.get('WX_MINI_SECRET', '')

    if not appid or not secret:
        # 开发模式：无微信配置时自动创建/登录测试用户
        user = User.query.filter_by(username='mini_user').first()
        if not user:
            user = User(
                username=f'mini_user_{code[:8]}',
                email=f'mini_{code[:8]}@paodinglaw.com',
                phone=None
            )
            user.set_password(code[:16])
            db.session.add(user)
            db.session.commit()

        token = generate_token(user.id)
        return jsonify({
            'token': token,
            'user': {
                'id': user.id,
                'username': user.username,
                'phone': user.phone or '',
                'email': user.email,
                'is_admin': user.is_admin,
                'is_lawyer': user.is_lawyer
            }
        })

    # 生产环境：调用微信接口
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
            return jsonify({'error': '微信登录失败'}), 400

        openid = result['openid']
        # 查找或创建用户
        user = User.query.filter_by(openid=openid).first()
        if not user:
            user = User(
                username=f'微信用户_{openid[-6:]}',
                email=f'{openid}@wechat.paodinglaw.com',
                openid=openid,
                phone=None
            )
            user.set_password(openid[:16])
            db.session.add(user)
            db.session.commit()

        token = generate_token(user.id)
        return jsonify({
            'token': token,
            'user': {
                'id': user.id,
                'username': user.username,
                'phone': user.phone or '',
                'email': user.email,
                'is_admin': user.is_admin,
                'is_lawyer': user.is_lawyer
            }
        })
    except Exception as e:
        current_app.logger.error(f'微信登录出错: {e}')
        return jsonify({'error': '登录服务异常'}), 500


@mini_app.route('/api/wechat/bind-phone', methods=['POST'])
@token_required
def wechat_bind_phone(current_user):
    """
    绑定手机号到微信账号
    使用微信小程序的 getPhoneNumber 返回的加密数据
    注意：需要微信小程序的 appid 和 secret
    """
    data = request.get_json() or {}
    encrypted_data = data.get('encryptedData', '')
    iv = data.get('iv', '')

    if not encrypted_data or not iv:
        return jsonify({'error': '参数不完整'}), 400

    appid = os.environ.get('WX_MINI_APPID', '')
    secret = os.environ.get('WX_MINI_SECRET', '')

    if not appid or not secret:
        return jsonify({'error': '微信配置未完成'}), 500

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend

        # 获取 session_key（需要额外存储 session_key）
        # 简化处理：直接让用户输入手机号验证
        return jsonify({'error': '请使用短信验证绑定手机号'}), 400
    except Exception as e:
        current_app.logger.error(f'绑定手机号出错: {e}')
        return jsonify({'error': '绑定失败'}), 500


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
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    phone = data.get('phone', '').strip()
    password = data.get('password', '').strip()
    sms_code = data.get('sms_code', '').strip()

    if not username:
        return jsonify({'error': '请输入用户名'}), 400
    if not phone or not password:
        return jsonify({'error': '请填写手机号和密码'}), 400
    if len(password) < 6:
        return jsonify({'error': '密码至少6位'}), 400

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

    # 创建用户
    user = User(
        username=username,
        email=f'{phone}@paodinglaw.com',
        phone=phone
    )
    user.set_password(password)
    db.session.add(user)
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
        'is_admin': user.is_admin,
        'is_lawyer': user.is_lawyer
    }
