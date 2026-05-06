from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone
from datetime import timedelta

# 上海时区 (UTC+8)
def _now():
    return datetime.utcnow() + timedelta(hours=8)

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120))
    password_hash = db.Column(db.String(256), nullable=False)
    phone = db.Column(db.String(20), unique=True)
    openid = db.Column(db.String(64), unique=True)
    wx_session_key = db.Column(db.String(64))
    is_admin = db.Column(db.Boolean, default=False)
    is_lawyer = db.Column(db.Boolean, default=False)
    balance = db.Column(db.Integer, default=0)  # 余额，单位：分
    created_at = db.Column(db.DateTime, default=_now)

    consultations = db.relationship('Consultation', backref='client',
                                    foreign_keys='Consultation.user_id', lazy='dynamic')

    def set_password(self, password):
        # 使用 pbkdf2 而非 scrypt（兼容 Python 3.9 无 scrypt 的环境）
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256:600000')

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Consultation(db.Model):
    __tablename__ = 'consultations'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending')  # pending, active, closed
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    lawyer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                           onupdate=_now)

    lawyer = db.relationship('User', foreign_keys=[lawyer_id])
    messages = db.relationship('Message', backref='consultation', lazy='dynamic',
                               order_by='Message.created_at')


class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    consultation_id = db.Column(db.Integer, db.ForeignKey('consultations.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    content = db.Column(db.Text)
    file_url = db.Column(db.String(500))
    file_type = db.Column(db.String(50))
    is_system = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_now)

    sender = db.relationship('User', foreign_keys=[sender_id])


class LegalDocument(db.Model):
    __tablename__ = 'legal_documents'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    category = db.Column(db.String(100), default='general')
    content = db.Column(db.Text, nullable=False)
    is_published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now,
                           onupdate=_now)


class SiteSetting(db.Model):
    __tablename__ = 'site_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=False)

    @classmethod
    def get(cls, key, default=''):
        s = cls.query.filter_by(key=key).first()
        return s.value if s else default

    @classmethod
    def set(cls, key, value):
        s = cls.query.filter_by(key=key).first()
        if s:
            s.value = value
        else:
            s = cls(key=key, value=value)
            db.session.add(s)
        db.session.commit()


class RechargeOrder(db.Model):
    """充值订单"""
    __tablename__ = 'recharge_orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    order_no = db.Column(db.String(64), unique=True, nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)  # 分
    payment_method = db.Column(db.String(20), default='manual')  # alipay/wechat/manual
    status = db.Column(db.String(20), default='pending')  # pending/success/failed
    voucher_url = db.Column(db.String(500))  # 转账凭证图片
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # 审核人
    remark = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    user = db.relationship('User', foreign_keys=[user_id])
    admin = db.relationship('User', foreign_keys=[admin_id])


class Transaction(db.Model):
    """交易记录"""
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    type = db.Column(db.String(20), nullable=False)  # recharge/consume/refund/adjust
    amount = db.Column(db.Integer, nullable=False)  # 分（正数收入，负数支出）
    balance_before = db.Column(db.Integer, default=0)
    balance_after = db.Column(db.Integer, default=0)
    order_id = db.Column(db.Integer, db.ForeignKey('recharge_orders.id'), nullable=True)
    service_type = db.Column(db.String(50))  # 消费时记录的服务类型
    description = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=_now)

    user = db.relationship('User', foreign_keys=[user_id])
    order = db.relationship('RechargeOrder', foreign_keys=[order_id])


class ServicePrice(db.Model):
    """服务价格"""
    __tablename__ = 'service_prices'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(30), nullable=False, default='consult')  # consult/contract/document/other
    price = db.Column(db.Integer, nullable=False, default=0)  # 分
    description = db.Column(db.String(500))
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)


class Invoice(db.Model):
    """发票"""
    __tablename__ = 'invoices'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)  # 发票抬头
    tax_id = db.Column(db.String(50))  # 税号
    amount = db.Column(db.Integer, nullable=False, default=0)  # 分
    order_ids = db.Column(db.String(500))  # 关联订单ID，逗号分隔
    status = db.Column(db.String(20), default='pending')  # pending/issued/cancelled
    file_url = db.Column(db.String(500))  # 发票文件
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    user = db.relationship('User', foreign_keys=[user_id])


class ServiceOrder(db.Model):
    """服务订单（用于计费）"""
    __tablename__ = 'service_orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    service_type = db.Column(db.String(50), nullable=False)
    price_id = db.Column(db.Integer, db.ForeignKey('service_prices.id'), nullable=True)
    amount = db.Column(db.Integer, default=0)  # 分
    status = db.Column(db.String(20), default='pending')  # pending/paid/completed/refunded
    consultation_id = db.Column(db.Integer, db.ForeignKey('consultations.id'), nullable=True)
    lawyer_reply_count = db.Column(db.Integer, default=0)  # 律师回复计数
    description = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    user = db.relationship('User', foreign_keys=[user_id])
    price = db.relationship('ServicePrice', foreign_keys=[price_id])
    consultation = db.relationship('Consultation', foreign_keys=[consultation_id])
