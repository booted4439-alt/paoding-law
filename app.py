import os
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_from_directory, abort)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from flask_socketio import SocketIO, join_room, emit
from werkzeug.utils import secure_filename

from config import Config
from models import db, User, Consultation, Message, LegalDocument, SiteSetting

# ---------- app init ----------
app = Flask(__name__)

app.config.from_object(Config)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'auth_login'
socketio = SocketIO(app, cors_allowed_origins="*")


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
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('index'))
        flash('用户名或密码错误', 'error')
    return render_template('login.html')


@app.route('/auth/register', methods=['GET', 'POST'])
def auth_register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if User.query.filter_by(username=username).first():
            flash('用户名已存在', 'error')
        elif User.query.filter_by(email=email).first():
            flash('邮箱已注册', 'error')
        else:
            user = User(username=username, email=email)
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
def consult_page():
    return render_template('consult.html')


@app.route('/api/consultations', methods=['GET'])
@login_required
def list_consultations():
    page = request.args.get('page', 1, type=int)
    status = request.args.get('status', '')
    q = Consultation.query.filter_by(user_id=current_user.id)
    if status:
        q = q.filter_by(status=status)
    q = q.order_by(Consultation.updated_at.desc())
    pagination = q.paginate(page=page, per_page=Config.ITEMS_PER_PAGE, error_out=False)
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


@app.route('/api/consultations/<int:c_id>', methods=['DELETE'])
@login_required
def delete_consultation(c_id):
    c = db.session.get(Consultation, c_id)
    if not c or c.user_id != current_user.id:
        return jsonify({'error': '未找到咨询'}), 404
    if c.status != 'pending':
        return jsonify({'error': '只能删除待处理的咨询'}), 400
    Message.query.filter_by(consultation_id=c.id).delete()
    db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True})


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


@app.route('/api/consultations/<int:c_id>/messages', methods=['POST'])
@login_required
def send_message(c_id):
    c = db.session.get(Consultation, c_id)
    if not c or (c.user_id != current_user.id and not current_user.is_lawyer):
        return jsonify({'error': '无权限'}), 403
    content = (request.form.get('content') or '').strip()
    file_url = None
    file_type = None
    if 'file' in request.files:
        f = request.files['file']
        if f and f.filename and allowed_file(f.filename):
            ext = f.filename.rsplit('.', 1)[1].lower()
            filename = f'{uuid.uuid4().hex}.{ext}'
            f.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            file_url = url_for('uploaded_file', filename=filename)
            file_type = ext
    if not content and not file_url:
        return jsonify({'error': '请输入内容或上传文件'}), 400
    msg = Message(consultation_id=c_id, sender_id=current_user.id,
                  content=content, file_url=file_url, file_type=file_type)
    db.session.add(msg)
    if c.status == 'pending':
        c.status = 'active'
    c.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    # real-time broadcast
    socketio.emit('new_message', {
        'consultation_id': c_id,
        'message': {
            'id': msg.id,
            'sender': current_user.username,
            'sender_id': current_user.id,
            'content': msg.content,
            'file_url': msg.file_url,
            'file_type': msg.file_type,
            'is_system': False,
            'created_at': msg.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        }
    }, room=f'consult_{c_id}')
    return jsonify({'ok': True, 'message_id': msg.id}), 201


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
    return jsonify({
        'id': d.id,
        'title': d.title,
        'category': d.category,
        'content': d.content,
        'updated_at': d.updated_at.strftime('%Y-%m-%d'),
    })


# ===================== ADMIN =====================
@app.route('/admin')
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
    keys = ['site_name', 'wechat_qr', 'address', 'phone',
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


# ===================== SOCKET.IO =====================
@socketio.on('join')
def handle_join(data):
    consultation_id = data.get('consultation_id')
    if consultation_id:
        join_room(f'consult_{consultation_id}')


# ===================== RUN =====================
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
