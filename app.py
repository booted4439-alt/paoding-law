import os
import uuid
from datetime import datetime, timezone
from functools import wraps

# 加载 .env 配置文件（本地开发用，服务端没有 dotenv 也不影响）
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path)
except ImportError:
    pass

# Eventlet monkey-patch for async workers

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_from_directory, abort)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from flask_socketio import SocketIO, join_room, emit
import pymysql
pymysql.install_as_MySQLdb()

from werkzeug.utils import secure_filename

from config import Config
from models import db, User, Consultation, Message, LegalDocument, SiteSetting
from models import RechargeOrder, Transaction, ServicePrice, Invoice, ServiceOrder
from services.sms import send_sms_code, check_sms_code, generate_code
from services.mailer import notify_new_message

# ---------- app init ----------
app = Flask(__name__)

app.config.from_object(Config)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'auth_login'
login_manager.login_message = ''
socketio = SocketIO(app, cors_allowed_origins="*")

# Register mini-program API blueprint
from miniapp_routes import mini_app
from miniapp_routes import verify_token as mini_verify_token
app.register_blueprint(mini_app)


# ---------- before_request: Bearer token → Flask-Login ----------
@app.before_request
def auto_login_from_bearer_token():
    """Bearer Token 登录（小程序用）"""
    if current_user.is_authenticated:
        return
    auth = request.headers.get('Authorization', '').strip()
    if auth.startswith('Bearer '):
        token = auth[7:]
        user_id = mini_verify_token(token)
        if user_id:
            user = db.session.get(User, int(user_id))
            if user:
                login_user(user)


# ---------- helpers ----------
def get_setting(key, default=''):
    return SiteSetting.get(key, default)


def validate_password(pwd):
    """密码只能包含字母和数字"""
    import re
    return bool(re.match(r'^[a-zA-Z0-9]+$', pwd))


def allowed_file(filename):
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*a, **kw)
    return wrapper


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.context_processor
def inject_globals():
    return dict(get_setting=get_setting)


# ---------- create tables & seed ----------
@app.cli.command('init-db')
def init_db():
    db.create_all()
    # seed admin
    if not User.query.filter_by(username='admin').first():
        admin = User(username='admin', email='admin@paodinglaw.com',
                     phone='13800000000', is_admin=True, is_lawyer=True)
        admin.set_password('admin123')
        db.session.add(admin)
    # seed default settings
    defaults = {
        'site_name': '庖丁法律服务',
        'wechat_qr': '',
        'address': '上海市浦东新区陆家嘴环路1000号',
        'phone': '021-6888-8888',
        'email': 'contact@paodinglaw.com',
        'icp_beian': '沪ICP备2025XXXXXX号',
        'police_beian': '',
        'privacy_content': '# 隐私政策\n\n我们重视您的隐私...',
        'terms_content': '# 用户协议\n\n**更新日期：2026年5月**\n\n## 一、总则\n\n### 1.1 平台性质\n庖丁法律服务（以下简称"本平台"）是由执业律师团队运营的法律服务平台，提供法律咨询、合同审核、文书代写、案件代理等法律服务。\n\n### 1.2 协议范围\n本协议是您与本平台之间关于使用服务所订立的协议。使用本平台服务即表示您已阅读、理解并同意本协议的全部内容。\n\n## 二、用户注册与账号管理\n\n### 2.1 注册条件\n您需提供真实、准确的手机号码完成注册。每个手机号限注册一个账号。\n\n### 2.2 账号安全\n您应对账号下的所有行为负责。如发现账号被盗用，请立即通知我们。\n\n### 2.3 账号注销\n您可以申请注销账号。注销后，我们将依法删除您的个人信息。\n\n## 三、服务内容与计费\n\n### 3.1 服务模式\n本平台采用"AI辅助+律师审核确认回复"的服务模式。AI提供基础法律分析，执业律师审核确认后向您回复。\n\n### 3.2 服务项目\n具体服务项目及价格以本平台公布的《价格》页面为准，包括但不限于：\n- 法律咨询（普通/复杂）\n- 合同审核（普通/复杂）\n- 合同起草（普通/复杂）\n- 文书代写（起诉状、答辩状、上诉状等）\n- 其他法律服务\n\n### 3.3 计费方式\n- 按次计费：1次 = 100元\n- 充值余额永久有效\n- 复杂/普通服务由律师评估后确定\n\n\n### 3.6 收费规则\n- 用户发起咨询后，律师根据咨询内容预估价格并通过咨询消息告知用户\n- 用户需充值不低于预估价格，以保证咨询服务的正常进行\n- 服务完成后，根据(律师回复次数 - 剔除次数) × 100元 扣费\n- 剔除次数由律师判断，为非专业性法律服务信息的回复（如寒暄、确认等），不计入收费\n- 充值低于预估价格的，除非经济特别困难且提供相关证明，否则不提供法律服务\n- 实际扣费以咨询完成时核算的（律师回复次数 - 剔除次数）× 100元 为准\n\n### 3.4 充值\n- 您可以通过微信转账、支付宝等方式充值\n- 企业套餐充值赠送规则以《价格》页面为准\n- 新注册用户可获赠300元体验金\n- 赠送金额不可申请退费\n\n### 3.5 发票\n服务完成后，您可在账单页面申请开具发票。\n\n## 四、用户行为规范\n\n### 4.1 合法使用\n您不得利用本平台从事任何违法违规活动，不得发布虚假、欺诈信息。\n\n### 4.2 知识产权\n本平台提供的法律文书、分析报告等内容的著作权归本平台所有。\n\n### 4.3 保密义务\n您与律师之间的咨询内容受律师-客户保密特权保护，我们将严格保密。\n\n## 五、免责声明\n\n### 5.1 服务性质\n本平台提供的法律分析和意见仅供参考，不构成正式的法律意见书。如需出具正式法律意见或委托代理案件，需另行签订委托代理合同。\n\n### 5.2 不可抗力\n因不可抗力导致的服务中断，本平台不承担责任。\n\n### 5.3 责任限制\n在法律允许的最大范围内，本平台对您的损失承担的责任不超过您支付的服务费用。\n\n## 六、协议的变更与终止\n\n### 6.1 协议变更\n我们可能根据法律法规变化或业务需要修订本协议。修订后的协议将在平台上公示。\n\n### 6.2 服务终止\n如您严重违反本协议，我们有权暂停或终止您的服务。\n\n## 七、法律适用与争议解决\n\n### 7.1 法律适用\n本协议的订立、执行和解释适用中华人民共和国法律。\n\n### 7.2 争议解决\n因本协议引起的争议，双方应友好协商解决；协商不成的，提交本平台运营方所在地有管辖权的人民法院诉讼解决。\n\n## 八、联系方式\n\n- 邮箱：2878071631@qq.com\n- 电话：15701593315\n- 地址：上海市长宁区中山西路1065号B座',
    }
    for k, v in defaults.items():
        if not SiteSetting.query.filter_by(key=k).first():
            SiteSetting.set(k, v)
    db.session.commit()
    print('Database initialized.')


# ===================== AUTH =====================
@app.route('/auth/login', methods=['GET', 'POST'])
def auth_login():
    login_mode = 'password'
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        login_mode = request.form.get('login_mode', 'password')

        if login_mode == 'sms':
            # 验证码登录
            sms_code = request.form.get('sms_code', '').strip()
            if not sms_code:
                flash('请输入验证码', 'error')
                return render_template('login.html', login_mode='sms')
            from services.sms import check_sms_code
            verify_result = check_sms_code(phone, sms_code)
            if not (verify_result.get('success') and verify_result.get('verify_result') == 'PASS'):
                flash('验证码错误或已过期', 'error')
                return render_template('login.html', login_mode='sms')
            user = User.query.filter_by(phone=phone).first()
            if user:
                login_user(user)
                next_page = request.args.get('next')
                return redirect(next_page or url_for('index'))
            flash('该手机号未注册', 'error')
            return render_template('login.html', login_mode='sms')
        else:
            # 密码登录
            password = request.form.get('password', '')
            user = User.query.filter_by(phone=phone).first()
            if user and user.check_password(password):
                login_user(user)
                next_page = request.args.get('next')
                return redirect(next_page or url_for('index'))
            flash('手机号或密码错误', 'error')
    return render_template('login.html', login_mode=login_mode)


# ===================== 微信扫码登录（网站） =====================
@app.route('/auth/wechat/login')
def wechat_web_auth():
    """跳转到微信扫码页面"""
    appid = os.environ.get('WX_WEB_APPID', '')
    if not appid:
        flash('微信登录未配置', 'error')
        return redirect(url_for('auth_login'))
    redirect_uri = url_for('wechat_web_callback', _external=True)
    state = uuid.uuid4().hex[:8]
    # 保存 state 用于验证回调
    wx_url = (
        'https://open.weixin.qq.com/connect/qrconnect'
        f'?appid={appid}'
        f'&redirect_uri={redirect_uri}'
        '&response_type=code'
        '&scope=snsapi_login'
        f'&state={state}'
        '#wechat_redirect'
    )
    return redirect(wx_url)


@app.route('/auth/wechat/callback')
def wechat_web_callback():
    """微信扫码回调"""
    code = request.args.get('code', '')
    state = request.args.get('state', '')
    if not code:
        flash('微信登录失败', 'error')
        return redirect(url_for('auth_login'))

    appid = os.environ.get('WX_WEB_APPID', '')
    secret = os.environ.get('WX_WEB_SECRET', '')
    if not appid or not secret:
        flash('微信登录未配置', 'error')
        return redirect(url_for('auth_login'))

    try:
        import requests
        # 用 code 换取 access_token + openid
        resp = requests.get(
            'https://api.weixin.qq.com/sns/oauth2/access_token',
            params={
                'appid': appid,
                'secret': secret,
                'code': code,
                'grant_type': 'authorization_code'
            },
            timeout=10
        )
        data = resp.json()
        if 'openid' not in data:
            current_app.logger.error(f'微信网页登录失败: {data}')
            flash('微信登录失败', 'error')
            return redirect(url_for('auth_login'))

        openid = data['openid']
        access_token = data.get('access_token', '')

        # 获取用户信息
        user = User.query.filter_by(openid=openid).first()
        if not user:
            # 尝试获取微信昵称
            nickname = '用户'
            try:
                info_resp = requests.get(
                    'https://api.weixin.qq.com/sns/userinfo',
                    params={'access_token': access_token, 'openid': openid},
                    timeout=10
                )
                info = info_resp.json()
                if info.get('nickname'):
                    nickname = info['nickname']
            except Exception:
                pass
            user = User(
                username=nickname,
                email=f'{openid}@wechat.paodinglaw.com',
                openid=openid,
                phone=None
            )
            user.set_password(openid[:16])
            db.session.add(user)
            db.session.commit()

        login_user(user)
        flash('微信登录成功', 'success')
        return redirect(url_for('index'))
    except Exception as e:
        current_app.logger.error(f'微信网页登录异常: {e}')
        flash('微信登录异常', 'error')
        return redirect(url_for('auth_login'))


@app.route('/auth/register', methods=['GET', 'POST'])
def auth_register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        phone = request.form.get('phone', '').strip()
        sms_code = request.form.get('sms_code', '').strip()

        # 密码校验
        if password and not validate_password(password):
            flash('密码只能包含字母和数字', 'error')
            return render_template('register.html')

        # 核验短信验证码
        if phone and sms_code:
            verify_result = check_sms_code(phone, sms_code)
            if not (verify_result.get('success') and verify_result.get('verify_result') == 'PASS'):
                flash('短信验证码错误或已过期', 'error')
                return render_template('register.html')
        else:
            flash('请完成手机验证', 'error')
            return render_template('register.html')

        if phone and User.query.filter_by(phone=phone).first():
            flash('该手机号已注册', 'error')
        else:
            user = User(username=username, email=email, phone=phone, balance=30000)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            # 赠送300元
            order_no = 'ZS' + datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:8].upper()
            order = RechargeOrder(user_id=user.id, order_no=order_no, amount=30000,
                                  payment_method='gift', status='success', admin_id=None,
                                  remark='注册赠送')
            db.session.add(order)
            db.session.flush()
            tx = Transaction(user_id=user.id, type='recharge', amount=30000,
                             balance_before=0, balance_after=30000,
                             order_id=order.id, description='赠送')
            db.session.add(tx)
            db.session.commit()
            login_user(user)
            return redirect(url_for('index'))
    return render_template('register.html')


@app.route('/auth/logout')
@login_required
def auth_logout():
    logout_user()
    return redirect(url_for('index'))


# ===================== SMS VERIFICATION =====================
@app.route('/api/sms/send', methods=['POST'])
def sms_send():
    """发送短信验证码"""
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    if not phone or not phone.isdigit() or len(phone) != 11:
        return jsonify({'success': False, 'message': '请输入正确的手机号码'}), 400

    result = send_sms_code(phone)
    if result.get('success'):
        return jsonify({'success': True, 'message': result.get('message', '验证码已发送')})
    else:
        return jsonify({'success': False, 'message': result.get('message', '发送失败')}), 500


@app.route('/api/sms/verify', methods=['POST'])
def sms_verify():
    """核验短信验证码"""
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    code = data.get('code', '').strip()
    if not phone or not code:
        return jsonify({'success': False, 'message': '参数不完整'}), 400

    result = check_sms_code(phone, code)
    if result.get('success') and result.get('verify_result') == 'PASS':
        return jsonify({'success': True, 'message': '验证通过'})
    else:
        return jsonify({'success': False, 'message': '验证码错误或已过期'}), 400


# ===================== WECHAT LOGIN =====================
# ===================== PAGES =====================
@app.route('/logo-preview')
def logo_preview():
    return render_template('logo_preview.html')


@app.route('/sitemap.xml')
def sitemap():
    from flask import Response
    xml = render_template('sitemap.xml')
    return Response(xml, mimetype='application/xml')


@app.route('/robots.txt')
def robots():
    from flask import Response
    content = 'User-agent: *\nAllow: /\nSitemap: https://paodinglaw.com/sitemap.xml\n'
    return Response(content, mimetype='text/plain')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/privacy')
def privacy():
    import markdown
    content = get_setting('privacy_content')
    html = markdown.markdown(content, extensions=['markdown.extensions.fenced_code'])
    return render_template('static_page.html', title='隐私政策', content=html)


@app.route('/terms')
def terms():
    import markdown
    content = get_setting('terms_content')
    html = markdown.markdown(content, extensions=['markdown.extensions.fenced_code'])
    return render_template('static_page.html', title='用户协议', content=html)


@app.route('/contact')
def contact():
    return render_template('contact.html')


# ===================== PRICING =====================
@app.route('/pricing')
def pricing():
    prices = ServicePrice.query.filter_by(is_active=True).order_by(ServicePrice.sort_order).all()
    categories = {}
    for p in prices:
        categories.setdefault(p.category, []).append(p)
    return render_template('pricing.html', categories=categories)


# ===================== BALANCE =====================
@app.route('/balance')
@login_required
def balance():
    return render_template('balance.html')


@app.route('/balance/orders')
@login_required
def balance_orders():
    return render_template('orders.html')


@app.route('/balance/invoice', methods=['GET', 'POST'])
@login_required
def balance_invoice():
    from sqlalchemy import func
    invoices = Invoice.query.filter_by(user_id=current_user.id).order_by(Invoice.created_at.desc()).all()
    # 计算可开票金额（充值总额 - 赠送总额 - 已开票金额）
    total_recharge = db.session.query(func.sum(Transaction.amount)).filter(
        Transaction.user_id == current_user.id,
        Transaction.type == 'recharge',
        Transaction.description != '赠送'
    ).scalar() or 0
    total_invoiced = db.session.query(func.sum(Invoice.amount)).filter(
        Invoice.user_id == current_user.id,
        Invoice.status.in_(['pending', 'issued'])
    ).scalar() or 0
    invoiceable = total_recharge - total_invoiced

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        tax_id = request.form.get('tax_id', '').strip()
        amount_str = request.form.get('amount', '0').strip()
        order_ids = request.form.get('order_ids', '').strip()
        try:
            amount = int(float(amount_str) * 100)
        except ValueError:
            amount = 0
        if not title:
            flash('请输入发票抬头', 'error')
            return render_template('invoice.html', invoices=invoices, invoiceable=invoiceable)
        if amount <= 0:
            flash('请输入正确的开票金额', 'error')
            return render_template('invoice.html', invoices=invoices, invoiceable=invoiceable)
        if amount > invoiceable:
            flash(f'可开票金额为 {invoiceable/100:.2f} 元（不含赠送部分），请调整金额', 'error')
            return render_template('invoice.html', invoices=invoices, invoiceable=invoiceable)
        inv = Invoice(user_id=current_user.id, title=title, tax_id=tax_id,
                      amount=amount, order_ids=order_ids, status='pending')
        db.session.add(inv)
        db.session.commit()
        flash('开票申请已提交，请等待审核', 'success')
        return redirect(url_for('balance_invoice'))
    return render_template('invoice.html', invoices=invoices, invoiceable=invoiceable)


@app.route('/balance/top_up', methods=['GET', 'POST'])
@login_required
def balance_top_up():
    if request.method == 'POST':
        amount_str = request.form.get('amount', '0').strip()
        payment_method = request.form.get('payment_method', 'manual')
        remark = request.form.get('remark', '').strip()
        try:
            amount = int(float(amount_str) * 100)
        except ValueError:
            flash('请输入正确的金额', 'error')
            return render_template('top_up.html', env={'alipay_configured': bool(os.environ.get('ALIPAY_APP_ID', ''))})
        if amount < 1:
            flash('请输入正确的金额', 'error')
            return render_template('top_up.html', env={'alipay_configured': bool(os.environ.get('ALIPAY_APP_ID', ''))})

        import uuid
        order_no = 'CZ' + datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:8].upper()

        # 支付宝支付：直接跳转支付页面
        if payment_method == 'alipay':
            from services.alipay import create_trade
            amount_yuan = amount / 100
            success, result = create_trade(
                order_no=order_no,
                subject='庖丁法律服务-充值',
                total_amount=amount_yuan,
                notify_url=app.config['ALIPAY_NOTIFY_URL'],
                return_url=app.config['ALIPAY_RETURN_URL']
            )
            if success:
                # 先创建订单
                order = RechargeOrder(user_id=current_user.id, order_no=order_no,
                                      amount=amount, payment_method='alipay',
                                      status='pending', remark=remark)
                db.session.add(order)
                db.session.commit()
                return redirect(result)
            else:
                flash(result, 'error')
                return render_template('top_up.html', env={'alipay_configured': bool(os.environ.get('ALIPAY_APP_ID', ''))})

        order = RechargeOrder(user_id=current_user.id, order_no=order_no,
                              amount=amount, payment_method=payment_method,
                              status='pending', remark=remark)

        # Handle voucher upload
        voucher = request.files.get('voucher')
        if voucher and voucher.filename:
            if allowed_file(voucher.filename):
                ext = voucher.filename.rsplit('.', 1)[1].lower()
                filename = f'voucher_{uuid.uuid4().hex}.{ext}'
                voucher.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                order.voucher_url = url_for('uploaded_file', filename=filename)

        db.session.add(order)
        db.session.commit()
        flash('充值申请已提交，请等待管理员审核', 'success')
        return redirect(url_for('balance_orders'))

    return render_template('top_up.html', env={
        'alipay_configured': bool(os.environ.get('ALIPAY_APP_ID', ''))
    })


# ===================== 支付宝支付回调 =====================

@app.route('/api/alipay/notify', methods=['POST'])
def alipay_notify():
    """支付宝异步通知回调"""
    import traceback, logging as logger
    logger.basicConfig(filename='/tmp/alipay_notify_error.log', level=logger.ERROR)
    try:
        from services.alipay import verify_notification
        data = request.form.to_dict()
        logger.info(f'notify data keys: {list(data.keys())}')
        success, verified_data = verify_notification(data)
        if not success:
            logger.error(f'verify failed, data keys: {list(data.keys())}')
            return 'failure'
        trade_status = verified_data.get('trade_status')
        if trade_status != 'TRADE_SUCCESS':
            logger.error(f'bad trade_status: {trade_status}')
            return 'failure'
        order_no = verified_data.get('out_trade_no', '')
        trade_no = verified_data.get('trade_no', '')
        total_amount_str = verified_data.get('total_amount', '0')
        total_amount = float(total_amount_str)
        if not order_no or not order_no.startswith('CZ'):
            logger.error(f'bad order_no: {order_no}')
            return 'failure'
        order = RechargeOrder.query.filter_by(order_no=order_no).first()
        if not order or order.status != 'pending':
            return 'success'
        amount = int(round(total_amount * 100))
        if order.amount != amount:
            logger.error(f'amount mismatch: order={order.amount}, calc={amount}, total={total_amount_str}')
            return 'failure'
        order.status = 'success'
        order.trade_no = trade_no
        order.admin_id = None
        user = User.query.get(order.user_id)
        if user:
            old_balance = user.balance or 0
            user.balance = old_balance + amount
        tx = Transaction(
            user_id=order.user_id, type='recharge',
            amount=amount, balance_before=old_balance,
            balance_after=user.balance if user else 0,
            order_id=order.id, description=f'支付宝充值 {total_amount} 元'
        )
        db.session.add(tx)
        db.session.commit()
        logger.info(f'recharge auto-approved: order={order_no}, amount={amount}')
        return 'success'
    except Exception as e:
        logger.error(f'alipay_notify exception: {e}\n{traceback.format_exc()}')
        return 'failure'


@app.route('/balance/top_up/alipay/return')
@login_required
def alipay_return():
    """支付宝支付成功返回页"""
    out_trade_no = request.args.get('out_trade_no', '')
    trade_no = request.args.get('trade_no', '')
    total_amount = request.args.get('total_amount', '')
    flash('充值成功！', 'success')
    return redirect(url_for('balance_orders'))


@app.route('/api/balance')
@login_required
def api_balance():
    """余额和交易记录"""
    tx_type = request.args.get('type', '')
    page = request.args.get('page', 1, type=int)
    q = Transaction.query.filter_by(user_id=current_user.id)
    if tx_type:
        q = q.filter_by(type=tx_type)
    q = q.order_by(Transaction.created_at.desc())
    pagination = q.paginate(page=page, per_page=Config.ITEMS_PER_PAGE, error_out=False)
    return jsonify({
        'balance': current_user.balance,
        'transactions': [{
            'id': t.id,
            'type': t.type,
            'amount': t.amount,
            'balance_before': t.balance_before,
            'balance_after': t.balance_after,
            'service_type': t.service_type,
            'description': t.description,
            'created_at': t.created_at.strftime('%Y-%m-%d %H:%M'),
        } for t in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current': page,
    })


@app.route('/api/balance/recharge_orders')
@login_required
def api_recharge_orders():
    orders = RechargeOrder.query.filter_by(user_id=current_user.id).order_by(RechargeOrder.created_at.desc()).all()
    return jsonify([{
        'id': o.id,
        'order_no': o.order_no,
        'amount': o.amount,
        'payment_method': o.payment_method,
        'status': o.status,
        'voucher_url': o.voucher_url,
        'remark': o.remark,
        'created_at': o.created_at.strftime('%Y-%m-%d %H:%M'),
    } for o in orders])


# ===================== CONSULTATION =====================
@app.route('/consult')
@login_required
def consult_page():
    return render_template('consult.html')


@app.route('/api/consultations', methods=['GET'])
@login_required
def list_consultations():
    app.logger.warning(f'list_consultations called, user={current_user.username} auth={current_user.is_authenticated}')
    page = request.args.get('page', 1, type=int)
    status = request.args.get('status', '')
    q = request.args.get('q', '').strip()
    query = Consultation.query.filter_by(user_id=current_user.id)
    if status:
        query = query.filter_by(status=status)
    if q:
        # Search by title or message content
        query = query.filter(
            db.or_(
                Consultation.title.contains(q),
                Consultation.id.in_(
                    db.session.query(Message.consultation_id).filter(
                        Message.content.contains(q)
                    )
                )
            )
        )
    query = query.order_by(Consultation.updated_at.desc())
    pagination = query.paginate(page=page, per_page=Config.ITEMS_PER_PAGE, error_out=False)
    return jsonify({
        'consultations': [{
            'id': c.id,
            'title': c.title,
            'status': c.status,
            'lawyer': c.lawyer.username if c.lawyer else None,
            'message_count': c.messages.count(),
            'lawyer_reply_count': c.lawyer_reply_count or 0,
            'excluded_count': c.excluded_count or 0,
            'actual_fee': c.actual_fee or 0,
            'created_at': c.created_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at': c.updated_at.strftime('%Y-%m-%d %H:%M'),
        } for c in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current': page,
    })


@app.route('/api/consultations', methods=['POST'])
@login_required
def create_consultation():
    data = request.get_json() or {}
    title = data.get('title', '').strip()
    description = data.get('description', '').strip()
    if not title:
        return jsonify({'error': '请输入咨询标题'}), 400
    if not current_user.phone and not current_user.is_admin:
        return jsonify({'error': '请先绑定手机号'}), 403
    if not current_user.is_admin and not current_user.is_lawyer and current_user.balance < 10000:
        return jsonify({'error': '余额不足，咨询需账户余额不低于100元，请先充值'}), 403
    c = Consultation(title=title, description=description, user_id=current_user.id)
    db.session.add(c)
    db.session.flush()

    # 创建首条消息
    if description:
        msg = Message(consultation_id=c.id, sender_id=current_user.id, content=description)
        db.session.add(msg)
    db.session.commit()

    # 普通用户新建咨询 → 邮件通知
    if not current_user.is_admin and not current_user.is_lawyer and description:
        contact_email = SiteSetting.get('contact_email') or SiteSetting.get('email')
        if contact_email:
            try:
                from services.mailer import send_email
                subject = f'新咨询 - {title}'
                body = f'用户 {current_user.username}（{current_user.phone or "未绑定手机"}）提交了新咨询。\n\n标题: {title}\n内容: {description}\n\n查看详情：https://paodinglaw.com/admin/consultations/{c.id}'
                send_email(contact_email, subject, body)
            except Exception as e:
                print(f'[EMAIL] 通知发送失败: {e}')

    return jsonify({'id': c.id, 'title': c.title}), 201


@app.route('/api/consultations/with-message', methods=['POST'])
@login_required
def create_consultation_with_message():
    """Create a consultation with the first message (and optionally files) in one call."""
    content = (request.form.get('content') or '').strip()
    title = (request.form.get('title') or '').strip()
    if not title:
        title = content[:50] if content else '法律咨询'
    if not content:
        return jsonify({'error': '请输入您的问题'}), 400
    if not current_user.phone and not current_user.is_admin:
        return jsonify({'error': '请先绑定手机号'}), 403
    if not current_user.is_admin and not current_user.is_lawyer and current_user.balance < 10000:
        return jsonify({'error': '余额不足，咨询需账户余额不低于100元，请先充值'}), 403

    # Validate limits
    files = request.files.getlist('file')
    err = check_message_limits(content, files)
    if err:
        return jsonify({'error': err}), 400

    c = Consultation(title=title, description=content, user_id=current_user.id)
    db.session.add(c)
    db.session.flush()

    # Create message for content
    if content:
        msg = Message(consultation_id=c.id, sender_id=current_user.id, content=content)
        db.session.add(msg)

    # Create messages for each file
    for f in files:
        result = save_uploaded_file(f)
        if result:
            file_url, file_type = result
            msg = Message(consultation_id=c.id, sender_id=current_user.id,
                          file_url=file_url, file_type=file_type,
                          content=f'[文件] {f.filename}')
            db.session.add(msg)

    c.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    # 普通用户新建咨询 → 邮件通知联系邮箱
    if not current_user.is_admin and not current_user.is_lawyer:
        contact_email = SiteSetting.get('contact_email') or SiteSetting.get('email')
        if contact_email:
            try:
                notify_new_message(c, msg, current_user, contact_email)
            except Exception as e:
                print(f'[EMAIL] 通知发送失败: {e}')

    # broadcast
    msgs = Message.query.filter_by(consultation_id=c.id).order_by(Message.created_at.asc()).all()
    for m in msgs:
        socketio.emit('new_message', {
            'consultation_id': c.id,
            'message': {
                'id': m.id,
                'sender': current_user.username,
                'sender_id': current_user.id,
                'content': m.content,
                'file_url': m.file_url,
                'file_type': m.file_type,
                'is_system': False,
                'created_at': m.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            }
        }, room=f'consult_{c.id}')

    return jsonify({'id': c.id, 'title': c.title}), 201


@app.route('/api/consultations/<int:c_id>', methods=['DELETE'])
@login_required
def delete_consultation(c_id):
    c = db.session.get(Consultation, c_id)
    if not c or (c.user_id != current_user.id and not current_user.is_admin):
        return jsonify({'error': '未找到咨询'}), 404
    Message.query.filter_by(consultation_id=c.id).delete()
    db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/consultations/<int:c_id>/close', methods=['POST'])
@login_required
def close_consultation(c_id):
    c = db.session.get(Consultation, c_id)
    if not c or (c.user_id != current_user.id and not current_user.is_admin):
        return jsonify({'error': '未找到咨询'}), 404
    if c.status == 'closed':
        return jsonify({'error': '咨询已关闭'}), 400
    c.status = 'closed'
    c.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'ok': True, 'status': 'closed'})


@app.route('/api/consultations/<int:c_id>', methods=['GET'])
@login_required
def get_consultation(c_id):
    c = db.session.get(Consultation, c_id)
    if not c or (c.user_id != current_user.id and not current_user.is_lawyer and not current_user.is_admin):
        return jsonify({'error': '无权限'}), 403
    return jsonify({
        'id': c.id,
        'title': c.title,
        'description': c.description,
        'status': c.status,
        'lawyer': c.lawyer.username if c.lawyer else None,
        'message_count': c.messages.count(),
        'lawyer_reply_count': c.lawyer_reply_count or 0,
        'excluded_count': c.excluded_count or 0,
        'actual_fee': c.actual_fee or 0,
        'created_at': c.created_at.strftime('%Y-%m-%d %H:%M'),
        'updated_at': c.updated_at.strftime('%Y-%m-%d %H:%M'),
    })


@app.route('/api/consultations/<int:c_id>/messages', methods=['GET'])
@login_required
def get_messages(c_id):
    c = db.session.get(Consultation, c_id)
    if not c or (c.user_id != current_user.id and not current_user.is_lawyer):
        return jsonify({'error': '无权限'}), 403
    msgs = c.messages.order_by(Message.created_at.asc()).all()
    return jsonify([{
        'id': m.id,
        'sender': m.sender.username,
        'sender_id': m.sender_id,
        'content': m.content,
        'file_url': m.file_url,
        'file_type': m.file_type,
        'is_system': m.is_system,
        'created_at': m.created_at.strftime('%Y-%m-%d %H:%M:%S'),
    } for m in msgs])


def check_message_limits(content, files):
    """Validate message content and file limits."""
    if len(content) > 6000:
        return '内容不能超过6000个字符'
    files_list = [f for f in files if f and f.filename]
    if len(files_list) > 50:
        return '附件不能超过50个'
    for f in files_list:
        f.seek(0, 2)  # seek to end
        size = f.tell()
        f.seek(0)  # seek back
        if size > 100 * 1024 * 1024:
            return f'附件 {f.filename} 超过100MB限制'
    return None


def save_uploaded_file(f):
    """Save a single uploaded file, return (url, type) or None."""
    if not f or not f.filename:
        return None
    if not allowed_file(f.filename):
        return None
    ext = f.filename.rsplit('.', 1)[1].lower()
    filename = f'{uuid.uuid4().hex}.{ext}'
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    return url_for('uploaded_file', filename=filename), ext


@app.route('/api/consultations/<int:c_id>/messages', methods=['POST'])
@login_required
def send_message(c_id):
    c = db.session.get(Consultation, c_id)
    if not c or (c.user_id != current_user.id and not current_user.is_lawyer):
        return jsonify({'error': '无权限'}), 403
    if not current_user.phone and not current_user.is_admin and not current_user.is_lawyer:
        return jsonify({'error': '请先绑定手机号'}), 403
    content = (request.form.get('content') or '').strip()
    files = request.files.getlist('file')

    # Validate limits
    err = check_message_limits(content, files)
    if err:
        return jsonify({'error': err}), 400

    if not content and not any(f.filename for f in files if f):
        return jsonify({'error': '请输入内容或上传文件'}), 400

    # Create message for text content
    if content:
        msg = Message(consultation_id=c_id, sender_id=current_user.id, content=content)
        db.session.add(msg)

    # Create messages for each file
    for f in files:
        result = save_uploaded_file(f)
        if result:
            file_url, file_type = result
            msg = Message(consultation_id=c_id, sender_id=current_user.id,
                          file_url=file_url, file_type=file_type,
                          content=f'[文件] {f.filename}')
            db.session.add(msg)

    if c.status == 'pending' and (current_user.is_admin or current_user.is_lawyer):
        c.status = 'active'
    c.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    # 普通用户发消息 → 通知联系邮箱
    if not current_user.is_admin and not current_user.is_lawyer:
        contact_email = SiteSetting.get('contact_email') or SiteSetting.get('email')
        if contact_email:
            try:
                notify_new_message(c, msg, current_user, contact_email)
            except Exception as e:
                print(f'[EMAIL] 通知失败: {e}')

    # 律师/管理员回复 → 计数 + 通知咨询发起用户
    if current_user.is_admin or current_user.is_lawyer:
        # 递增律师回复计数
        c.lawyer_reply_count = (c.lawyer_reply_count or 0) + 1
        db.session.commit()

        # 通知咨询发起用户（有邮箱的）
        owner = db.session.get(User, c.user_id)
        if owner and owner.email:
            try:
                from services.mailer import send_email
                subject = f'律师回复 - {c.title or "法律咨询"}'
                body = (
                    f'{current_user.username} 回复了您的咨询「{c.title or "法律咨询"}」。\n\n'
                    f'回复内容：{msg.content or "(文件消息)"}\n\n'
                    f'点击查看：https://paodinglaw.com/consult/{c.id}'
                )
                send_email(owner.email, subject, body)
            except Exception as e:
                print(f'[EMAIL] 通知用户失败: {e}')

    # real-time broadcast all new messages
    new_msgs = Message.query.filter_by(consultation_id=c_id).order_by(Message.id.desc()).limit(len(files) + (1 if content else 0)).all()
    for m in reversed(new_msgs):
        socketio.emit('new_message', {
            'consultation_id': c_id,
            'message': {
                'id': m.id,
                'sender': current_user.username,
                'sender_id': current_user.id,
                'content': m.content,
                'file_url': m.file_url,
                'file_type': m.file_type,
                'is_system': False,
                'created_at': m.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            }
        }, room=f'consult_{c_id}')

    return jsonify({'ok': True}), 201


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ===================== LEGAL DOCUMENTS =====================
@app.route('/documents')
def documents():
    categories = db.session.query(LegalDocument.category).filter(
        LegalDocument.is_published == True
    ).distinct().all()
    return render_template('documents.html', categories=[c[0] for c in categories])


@app.route('/api/documents')
def api_documents():
    q = LegalDocument.query.filter_by(is_published=True)
    kw = request.args.get('q', '').strip()
    cat = request.args.get('category', '').strip()
    if kw:
        q = q.filter(LegalDocument.title.contains(kw) |
                     LegalDocument.content.contains(kw))
    if cat:
        q = q.filter_by(category=cat)
    q = q.order_by(LegalDocument.updated_at.desc()).limit(200)
    return jsonify([{
        'id': d.id,
        'title': d.title,
        'category': d.category,
        'updated_at': d.updated_at.strftime('%Y-%m-%d'),
    } for d in q.all()])


@app.route('/api/documents/<int:d_id>')
def api_document_detail(d_id):
    d = db.session.get(LegalDocument, d_id)
    if not d or not d.is_published:
        return jsonify({'error': '文档未找到'}), 404

    # Render markdown to HTML on the server side
    rendered = d.content
    try:
        import markdown
        rendered = markdown.markdown(d.content, extensions=['extra', 'sane_lists'])
    except Exception:
        # fallback: simple line break conversion
        rendered = d.content.replace('\n', '<br>')

    return jsonify({
        'id': d.id,
        'title': d.title,
        'category': d.category,
        'content': d.content,
        'rendered': rendered,
        'updated_at': d.updated_at.strftime('%Y-%m-%d'),
    })


# ===================== ADMIN =====================
@app.route('/admin', strict_slashes=False)
@login_required
@admin_required
def admin_dashboard():
    return render_template('admin/dashboard.html')


@app.route('/admin/consultations')
@login_required
@admin_required
def admin_consultations():
    return render_template('admin/consultations.html')


@app.route('/admin/api/consultations')
@login_required
@admin_required
def admin_list_consultations():
    page = request.args.get('page', 1, type=int)
    status = request.args.get('status', '')
    q = Consultation.query
    if status:
        q = q.filter_by(status=status)
    q = q.order_by(Consultation.updated_at.desc())
    pagination = q.paginate(page=page, per_page=Config.ITEMS_PER_PAGE, error_out=False)
    return jsonify({
        'consultations': [{
            'id': c.id,
            'title': c.title,
            'status': c.status,
            'client': c.client.username if c.client else '未知',
            'lawyer': c.lawyer.username if c.lawyer else None,
            'message_count': c.messages.count(),
            'lawyer_reply_count': c.lawyer_reply_count or 0,
            'excluded_count': c.excluded_count or 0,
            'estimated_fee': c.estimated_fee or 0,
            'actual_fee': c.actual_fee or 0,
            'created_at': c.created_at.strftime('%Y-%m-%d %H:%M'),
        } for c in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current': page,
    })


@app.route('/admin/consultations/<int:c_id>')
@login_required
@admin_required
def admin_consultation_detail(c_id):
    c = db.session.get(Consultation, c_id)
    if not c:
        abort(404)
    return render_template('admin/consult_detail.html', consultation=c)


@app.route('/admin/api/consultations/<int:c_id>/assign', methods=['POST'])
@login_required
@admin_required
def admin_assign_lawyer(c_id):
    c = db.session.get(Consultation, c_id)
    if not c:
        return jsonify({'error': '未找到'}), 404
    data = request.get_json() or {}
    lawyer_id = data.get('lawyer_id')
    c.lawyer_id = lawyer_id
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/api/consultations/<int:c_id>/excluded', methods=['POST'])
@login_required
@admin_required
def admin_update_excluded_count(c_id):
    """Update excluded count for a consultation."""
    c = db.session.get(Consultation, c_id)
    if not c:
        return jsonify({'error': '未找到咨询'}), 404
    if c.status == 'completed':
        return jsonify({'error': '已完成咨询不能修改剔除次数'}), 400
    data = request.get_json() or {}
    val = data.get('excluded_count', 0)
    try:
        val = int(val)
    except (ValueError, TypeError):
        return jsonify({'error': '无效的数字'}), 400
    if val < 0:
        return jsonify({'error': '剔除次数不能为负数'}), 400
    if val > (c.lawyer_reply_count or 0):
        return jsonify({'error': '剔除次数不能超过律师回复次数'}), 400
    c.excluded_count = val
    db.session.commit()
    return jsonify({'ok': True, 'excluded_count': val})


@app.route('/admin/api/consultations/<int:c_id>/complete', methods=['POST'])
@login_required
@admin_required
def admin_complete_consultation(c_id):
    """Complete a consultation: calculate fee, deduct from user balance, record transaction."""
    c = db.session.get(Consultation, c_id)
    if not c:
        return jsonify({'error': '未找到咨询'}), 404
    if c.status == 'completed':
        return jsonify({'error': '咨询已完成'}), 400
    if c.status == 'closed':
        return jsonify({'error': '咨询已关闭'}), 400

    reply_count = c.lawyer_reply_count or 0
    excluded = c.excluded_count or 0
    payable = reply_count - excluded
    unit_price = 10000  # 100元/次，单位为分
    fee = payable * unit_price

    owner = db.session.get(User, c.user_id)
    if not owner:
        return jsonify({'error': '用户不存在'}), 404

    if fee > 0:
        if owner.balance < fee:
            return jsonify({'error': f'用户余额不足，需要 {fee/100:.0f} 元，当前余额 {owner.balance/100:.0f} 元'}), 400

        before = owner.balance
        owner.balance -= fee
        tx = Transaction(
            user_id=owner.id,
            type='consume',
            amount=-fee,
            balance_before=before,
            balance_after=owner.balance,
            service_type='consult',
            description=f'咨询完成扣费 {fee/100:.0f} 元（{reply_count}次律师回复 - {excluded}次剔除 = {payable}次×100元）'
        )
        db.session.add(tx)

    c.actual_fee = fee
    c.status = 'completed'
    c.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({'ok': True, 'fee': fee, 'fee_yuan': f'{fee/100:.0f}'})


@app.route('/admin/documents')
@login_required
@admin_required
def admin_documents():
    return render_template('admin/documents.html')


@app.route('/admin/api/documents', methods=['GET'])
@login_required
@admin_required
def admin_list_documents():
    q = LegalDocument.query.order_by(LegalDocument.updated_at.desc()).all()
    return jsonify([{
        'id': d.id,
        'title': d.title,
        'category': d.category,
        'is_published': d.is_published,
        'updated_at': d.updated_at.strftime('%Y-%m-%d'),
    } for d in q])


@app.route('/admin/api/documents/<int:d_id>', methods=['GET'])
@login_required
@admin_required
def admin_get_document(d_id):
    d = db.session.get(LegalDocument, d_id)
    if not d:
        return jsonify({'error': '未找到'}), 404
    return jsonify({
        'id': d.id,
        'title': d.title,
        'category': d.category,
        'content': d.content,
        'is_published': d.is_published,
        'updated_at': d.updated_at.strftime('%Y-%m-%d'),
    })


@app.route('/admin/api/documents', methods=['POST'])
@login_required
@admin_required
def admin_create_document():
    data = request.get_json() or {}
    d = LegalDocument(
        title=data.get('title', '').strip(),
        category=data.get('category', 'general').strip(),
        content=data.get('content', ''),
        is_published=data.get('is_published', True),
    )
    db.session.add(d)
    db.session.commit()
    return jsonify({'id': d.id}), 201


@app.route('/admin/api/documents/<int:d_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_document(d_id):
    d = db.session.get(LegalDocument, d_id)
    if not d:
        return jsonify({'error': '未找到'}), 404
    data = request.get_json() or {}
    for field in ('title', 'category', 'content'):
        if field in data:
            setattr(d, field, data[field])
    if 'is_published' in data:
        d.is_published = bool(data['is_published'])
    d.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/api/documents/<int:d_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_document(d_id):
    d = db.session.get(LegalDocument, d_id)
    if d:
        db.session.delete(d)
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/settings')
@login_required
@admin_required
def admin_settings():
    return render_template('admin/settings.html')


@app.route('/admin/api/settings', methods=['GET'])
@login_required
@admin_required
def admin_get_settings():
    keys = ['site_name', 'wechat_qr', 'address', 'phone', 'email',
            'icp_beian', 'police_beian', 'privacy_content', 'terms_content']
    return jsonify({k: SiteSetting.get(k) for k in keys})


@app.route('/admin/api/settings', methods=['POST'])
@login_required
@admin_required
def admin_save_settings():
    data = request.get_json() or {}
    for k, v in data.items():
        SiteSetting.set(k, v)
    return jsonify({'ok': True})


# ===================== ADMIN: RECHARGE MANAGEMENT =====================
@app.route('/admin/recharges')
@login_required
@admin_required
def admin_recharges():
    return render_template('admin/recharges.html')


@app.route('/admin/api/recharges')
@login_required
@admin_required
def admin_list_recharges():
    status = request.args.get('status', '')
    q = RechargeOrder.query
    if status:
        q = q.filter_by(status=status)
    q = q.order_by(RechargeOrder.created_at.desc()).all()
    users = {u.id: u for u in User.query.all()}
    return jsonify([{
        'id': o.id,
        'order_no': o.order_no,
        'user_id': o.user_id,
        'username': users[o.user_id].username if o.user_id in users else '未知',
        'phone': users[o.user_id].phone if o.user_id in users else '',
        'amount': o.amount,
        'payment_method': o.payment_method,
        'status': o.status,
        'voucher_url': o.voucher_url,
        'remark': o.remark,
        'admin_username': users[o.admin_id].username if o.admin_id and o.admin_id in users else None,
        'created_at': o.created_at.strftime('%Y-%m-%d %H:%M'),
    } for o in q])


@app.route('/admin/recharges/<int:oid>/approve', methods=['POST'])
@login_required
@admin_required
def admin_approve_recharge(oid):
    order = db.session.get(RechargeOrder, oid)
    if not order or order.status != 'pending':
        return jsonify({'error': '订单状态错误'}), 400
    order.status = 'success'
    order.admin_id = current_user.id

    user = db.session.get(User, order.user_id)
    before = user.balance
    user.balance += order.amount

    tx = Transaction(user_id=order.user_id, type='recharge', amount=order.amount,
                     balance_before=before, balance_after=user.balance,
                     order_id=order.id, description=f'充值 {order.amount/100:.2f} 元')
    db.session.add(tx)
    db.session.commit()
    return jsonify({'ok': True, 'new_balance': user.balance})


@app.route('/admin/recharges/<int:oid>/reject', methods=['POST'])
@login_required
@admin_required
def admin_reject_recharge(oid):
    order = db.session.get(RechargeOrder, oid)
    if not order or order.status != 'pending':
        return jsonify({'error': '订单状态错误'}), 400
    order.status = 'failed'
    order.admin_id = current_user.id
    data = request.get_json() or {}
    order.remark = data.get('remark', order.remark or '')
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/adjust_balance', methods=['POST'])
@login_required
@admin_required
def admin_adjust_balance():
    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form.to_dict()
    phone = data.get('phone')
    user_id = data.get('user_id')
    amount = data.get('amount', 0)  # 单位：分，可正可负
    reason = data.get('reason', '').strip()
    payment_method = data.get('payment_method', 'adjust')
    if phone:
        user = User.query.filter_by(phone=phone).first()
        if not user:
            return jsonify({'error': '未找到该手机号的用户'}), 404
    elif user_id:
        user = db.session.get(User, int(user_id))
    else:
        return jsonify({'error': '请输入用户手机号'}), 400
    if not user:
        return jsonify({'error': '用户未找到'}), 404
    before = user.balance
    user.balance += int(amount)
    if user.balance < 0:
        user.balance = 0
    tt = 'adjust' if amount >= 0 else 'refund'
    # 同时创建充值订单记录，便于在充值管理列表查看
    order_no = 'TZ' + datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:8].upper()
    order = RechargeOrder(
        user_id=user.id, order_no=order_no,
        amount=abs(int(amount)), payment_method=payment_method,
        status='success', admin_id=current_user.id,
        remark=reason or '管理员调账'
    )
    # Handle voucher upload
    voucher = request.files.get('voucher') if not request.is_json else None
    if voucher and voucher.filename:
        if allowed_file(voucher.filename):
            ext = voucher.filename.rsplit('.', 1)[1].lower()
            filename = f'voucher_{uuid.uuid4().hex}.{ext}'
            voucher.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            order.voucher_url = url_for('uploaded_file', filename=filename)
    db.session.add(order)
    db.session.flush()
    tx = Transaction(user_id=user.id, type=tt, amount=amount,
                     balance_before=before, balance_after=user.balance,
                     order_id=order.id, description=reason or '管理员调账')
    db.session.add(tx)
    db.session.commit()
    return jsonify({'ok': True, 'new_balance': user.balance})


# ===================== ADMIN: SERVICE PRICES =====================
@app.route('/admin/prices')
@login_required
@admin_required
def admin_prices():
    return render_template('admin/prices.html')


@app.route('/admin/api/prices', methods=['GET'])
@login_required
@admin_required
def admin_list_prices():
    prices = ServicePrice.query.order_by(ServicePrice.sort_order).all()
    return jsonify([{
        'id': p.id,
        'name': p.name,
        'category': p.category,
        'price': p.price,
        'description': p.description or '',
        'sort_order': p.sort_order,
        'is_active': p.is_active,
    } for p in prices])


@app.route('/admin/api/prices', methods=['POST'])
@login_required
@admin_required
def admin_create_price():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': '名称必填'}), 400
    price = ServicePrice(
        name=name,
        category=data.get('category', 'consult'),
        price=int(float(data.get('price', 0)) * 100),
        description=data.get('description', '').strip(),
        sort_order=data.get('sort_order', 0),
        is_active=data.get('is_active', True),
    )
    db.session.add(price)
    db.session.commit()
    return jsonify({'id': price.id}), 201


@app.route('/admin/api/prices/<int:pid>', methods=['PUT'])
@login_required
@admin_required
def admin_update_price(pid):
    price = db.session.get(ServicePrice, pid)
    if not price:
        return jsonify({'error': '未找到'}), 404
    data = request.get_json() or {}
    if 'name' in data:
        price.name = data['name'].strip()
    if 'category' in data:
        price.category = data['category']
    if 'price' in data:
        price.price = int(float(data['price']) * 100)
    if 'description' in data:
        price.description = data['description'].strip()
    if 'sort_order' in data:
        price.sort_order = data['sort_order']
    if 'is_active' in data:
        price.is_active = bool(data['is_active'])
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/api/prices/<int:pid>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_price(pid):
    price = db.session.get(ServicePrice, pid)
    if price:
        db.session.delete(price)
        db.session.commit()
    return jsonify({'ok': True})


# ===================== ADMIN: INVOICE MANAGEMENT =====================
@app.route('/admin/invoices')
@login_required
@admin_required
def admin_invoices():
    return render_template('admin/invoices.html')


@app.route('/admin/api/invoices', methods=['GET'])
@login_required
@admin_required
def admin_list_invoices():
    status = request.args.get('status', '')
    q = Invoice.query
    if status:
        q = q.filter_by(status=status)
    q = q.order_by(Invoice.created_at.desc()).all()
    users = {u.id: u for u in User.query.all()}
    return jsonify([{
        'id': inv.id,
        'user_id': inv.user_id,
        'username': users[inv.user_id].username if inv.user_id in users else '未知',
        'title': inv.title,
        'tax_id': inv.tax_id or '',
        'amount': inv.amount,
        'order_ids': inv.order_ids or '',
        'status': inv.status,
        'file_url': inv.file_url or '',
        'created_at': inv.created_at.strftime('%Y-%m-%d %H:%M'),
    } for inv in q])


@app.route('/admin/api/invoices/<int:iid>', methods=['PUT'])
@login_required
@admin_required
def admin_update_invoice(iid):
    inv = db.session.get(Invoice, iid)
    if not inv:
        return jsonify({'error': '未找到'}), 404
    data = request.get_json() or {}
    if 'status' in data:
        inv.status = data['status']
    if 'file_url' in data:
        inv.file_url = data['file_url']
    db.session.commit()
    return jsonify({'ok': True})


# ===================== ADMIN: USER MANAGEMENT =====================
@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    return render_template('admin/users.html')


@app.route('/admin/api/users', methods=['GET'])
@login_required
@admin_required
def admin_list_users():
    q = User.query.order_by(User.created_at.desc()).all()
    return jsonify([{
        'id': u.id,
        'username': u.username,
        'email': u.email,
        'phone': u.phone or '',
        'is_admin': u.is_admin,
        'is_lawyer': u.is_lawyer,
        'balance': u.balance or 0,
        'created_at': u.created_at.strftime('%Y-%m-%d %H:%M'),
    } for u in q])


@app.route('/admin/api/users', methods=['POST'])
@login_required
@admin_required
def admin_create_user():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '')
    phone = data.get('phone', '').strip() or None
    is_admin = data.get('is_admin', False)
    is_lawyer = data.get('is_lawyer', False)

    if not username or not password:
        return jsonify({'error': '用户名、密码为必填'}), 400
    if not validate_password(password):
        return jsonify({'error': '密码只能包含字母和数字'}), 400
    if phone and User.query.filter_by(phone=phone).first():
        return jsonify({'error': '手机号已注册'}), 400

    user = User(username=username, email=email, phone=phone,
                is_admin=is_admin, is_lawyer=is_lawyer)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify({'ok': True, 'id': user.id}), 201


@app.route('/admin/api/users/<int:u_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_user(u_id):
    user = db.session.get(User, u_id)
    if not user:
        return jsonify({'error': '用户未找到'}), 404

    data = request.get_json() or {}
    if 'username' in data and data['username'].strip():
        u = User.query.filter_by(username=data['username'].strip()).first()
        if u and u.id != user.id:
            return jsonify({'error': '用户名已存在'}), 400
        user.username = data['username'].strip()
    if 'email' in data:
        user.email = data['email'].strip() or None
    if 'phone' in data:
        p = data['phone'].strip() or None
        if p:
            u = User.query.filter_by(phone=p).first()
            if u and u.id != user.id:
                return jsonify({'error': '手机号已注册'}), 400
        user.phone = p
    if 'password' in data and data['password']:
        user.set_password(data['password'])
    if 'is_admin' in data:
        user.is_admin = bool(data['is_admin'])
    if 'is_lawyer' in data:
        user.is_lawyer = bool(data['is_lawyer'])

    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/api/users/<int:u_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_user(u_id):
    if u_id == current_user.id:
        return jsonify({'error': '不能删除自己'}), 400
    user = db.session.get(User, u_id)
    if not user:
        return jsonify({'error': '用户未找到'}), 404
    # Delete associated consultations and messages
    cons = Consultation.query.filter_by(user_id=u_id).all()
    for c in cons:
        Message.query.filter_by(consultation_id=c.id).delete()
        db.session.delete(c)
    # Remove lawyer assignments
    Consultation.query.filter_by(lawyer_id=u_id).update({Consultation.lawyer_id: None})
    # Delete recharge orders and related transactions
    orders = RechargeOrder.query.filter_by(user_id=u_id).all()
    for o in orders:
        Transaction.query.filter_by(order_id=o.id).delete()
        db.session.delete(o)
    # Clean other foreign key references
    Transaction.query.filter_by(user_id=u_id).delete()
    Invoice.query.filter_by(user_id=u_id).delete()
    ServiceOrder.query.filter_by(user_id=u_id).delete()
    RechargeOrder.query.filter_by(admin_id=u_id).update({RechargeOrder.admin_id: None})
    db.session.delete(user)
    db.session.commit()
    return jsonify({'ok': True})


# ===================== SOCKET.IO =====================
@socketio.on('join')
def handle_join(data):
    consultation_id = data.get('consultation_id')
    if consultation_id:
        join_room(f'consult_{consultation_id}')


# ===================== RUN =====================


@app.errorhandler(401)
def unauthorized(e):
    """401：API 请求返回 JSON，页面请求跳转登录页"""
    if request.path.startswith('/api/'):
        return jsonify({'error': '未登录或登录已过期'}), 401
    return redirect(url_for('auth_login', next=request.path))


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True, use_reloader=False, host='0.0.0.0', port=5000)
