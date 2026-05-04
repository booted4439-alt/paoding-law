import os
import uuid
from datetime import datetime, timezone
from functools import wraps

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
from services.sms import send_sms_code, check_sms_code, generate_code

# ---------- app init ----------
app = Flask(__name__)

app.config.from_object(Config)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'auth_login'
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
                     is_admin=True, is_lawyer=True)
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
        'terms_content': '# 用户协议\n\n欢迎使用庖丁法律服务...',
    }
    for k, v in defaults.items():
        if not SiteSetting.query.filter_by(key=k).first():
            SiteSetting.set(k, v)
    db.session.commit()
    print('Database initialized.')


# ===================== AUTH =====================
@app.route('/auth/login', methods=['GET', 'POST'])
def auth_login():
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(phone=phone).first()
        if user and user.check_password(password):
            login_user(user)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('index'))
        flash('手机号或密码错误', 'error')
    return render_template('login.html')


@app.route('/auth/register', methods=['GET', 'POST'])
def auth_register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        phone = request.form.get('phone', '').strip()
        sms_code = request.form.get('sms_code', '').strip()

        # 核验短信验证码
        if phone and sms_code:
            verify_result = check_sms_code(phone, sms_code)
            if not (verify_result.get('success') and verify_result.get('verify_result') == 'PASS'):
                flash('短信验证码错误或已过期', 'error')
                return render_template('register.html')
        else:
            flash('请完成手机验证', 'error')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('邮箱已注册', 'error')
        elif User.query.filter_by(phone=phone).first():
            flash('该手机号已注册', 'error')
        else:
            user = User(username=username, email=email, phone=phone)
            user.set_password(password)
            db.session.add(user)
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
        return jsonify({'success': True, 'message': '验证码已发送'})
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


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/privacy')
def privacy():
    content = get_setting('privacy_content')
    return render_template('static_page.html', title='隐私政策', content=content)


@app.route('/terms')
def terms():
    content = get_setting('terms_content')
    return render_template('static_page.html', title='用户协议', content=content)


@app.route('/contact')
def contact():
    return render_template('contact.html')


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
    c = Consultation(title=title, description=description, user_id=current_user.id)
    db.session.add(c)
    db.session.commit()
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

    c.status = 'active'
    c.updated_at = datetime.now(timezone.utc)
    db.session.commit()

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

    if c.status == 'pending':
        c.status = 'active'
    c.updated_at = datetime.now(timezone.utc)
    db.session.commit()

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
        'summary': d.summary,
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
        'summary': d.summary,
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
        summary=data.get('summary', '').strip(),
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
    for field in ('title', 'category', 'content', 'summary'):
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

    if not username or not email or not password:
        return jsonify({'error': '用户名、邮箱、密码为必填'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': '邮箱已注册'}), 400
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
    if 'email' in data and data['email'].strip():
        u = User.query.filter_by(email=data['email'].strip()).first()
        if u and u.id != user.id:
            return jsonify({'error': '邮箱已注册'}), 400
        user.email = data['email'].strip()
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
    # Delete associated consultations and messages first
    cons = Consultation.query.filter_by(user_id=u_id).all()
    for c in cons:
        Message.query.filter_by(consultation_id=c.id).delete()
        db.session.delete(c)
    # Remove lawyer assignments
    Consultation.query.filter_by(lawyer_id=u_id).update({Consultation.lawyer_id: None})
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
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
