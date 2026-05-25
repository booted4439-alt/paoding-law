import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def get_database_uri():
    """获取数据库URI，优先使用环境变量，默认SQLite"""
    db_url = os.environ.get('DATABASE_URL')
    if db_url:
        return db_url
    # MySQL配置
    mysql_host = os.environ.get('MYSQL_HOST', '127.0.0.1')
    mysql_port = os.environ.get('MYSQL_PORT', '3306')
    mysql_user = os.environ.get('MYSQL_USER', 'root')
    mysql_pass = os.environ.get('MYSQL_PASSWORD', '')
    mysql_db = os.environ.get('MYSQL_DATABASE', 'paoding_law')
    if mysql_pass:
        return f'mysql+pymysql://{mysql_user}:{mysql_pass}@{mysql_host}:{mysql_port}/{mysql_db}?charset=utf8mb4'
    return 'sqlite:///' + os.path.join(BASE_DIR, 'paoding.db')


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'paoding-law-secret-key-2026')
    SQLALCHEMY_DATABASE_URI = get_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500MB (limit from per-file * 50 files)
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp',
                          'pdf', 'doc', 'docx', 'txt', 'xls', 'xlsx'}
    ITEMS_PER_PAGE = 20

    # 支付宝配置
    ALIPAY_NOTIFY_URL = os.environ.get('ALIPAY_NOTIFY_URL', 'https://calculuslaw.com/api/alipay/notify')
    ALIPAY_RETURN_URL = os.environ.get('ALIPAY_RETURN_URL', 'https://calculuslaw.com/balance/top_up/alipay/return')
