#!/bin/bash
# 庖丁法律服务 - 生产部署脚本
set -e

APP_DIR="/var/www/paodinglaw"
PYTHON="/usr/local/python310/bin/python3"
NGINX_CONF="/etc/nginx/conf.d/paodinglaw.com.conf"
DOMAIN="paodinglaw.com"

echo "🔪 部署庖丁法律服务..."

# 1. 拉取最新代码
cd "$APP_DIR"
git fetch origin
git reset --hard origin/main

# 2. 安装依赖
echo "📦 安装Python依赖..."
$PYTHON -m pip install --upgrade pip --quiet
$PYTHON -m pip install -r requirements.txt --quiet
$PYTHON -m pip install gunicorn eventlet --quiet

# 3. 创建数据库（如果不存在）
echo "🗄️  初始化数据库..."
MYSQL_PASS="Bos123mvc!"
mysql -u root -p"$MYSQL_PASS" -e "CREATE DATABASE IF NOT EXISTS paoding_law DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" 2>/dev/null

# 4. 初始化数据库表
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_USER=root
export MYSQL_PASSWORD="$MYSQL_PASS"
export MYSQL_DATABASE=paoding_law
export FLASK_ENV=production

$PYTHON -c "
from app import app, db
with app.app_context():
    db.create_all()
print('数据库表创建完成')
"

# 5. 创建上传目录
mkdir -p static/uploads
chmod 755 static/uploads

# 6. 更新Nginx配置
echo "🔧 配置Nginx..."
cat > "$NGINX_CONF" << 'NGINXEOF'
server {
    listen 80;
    server_name paodinglaw.com www.paodinglaw.com;
    
    client_max_body_size 500M;

    # WebSocket支持 (Socket.IO)
    location /socket.io/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 后端API
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 管理后台
    location /admin {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 认证
    location /auth/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 上传文件
    location /uploads/ {
        alias /var/www/paodinglaw/static/uploads/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # 静态文件
    location /static/ {
        alias /var/www/paodinglaw/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # 首页及前端路由
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    access_log /var/log/nginx/paodinglaw_access.log;
    error_log /var/log/nginx/paodinglaw_error.log;
}
NGINXEOF

nginx -t && systemctl reload nginx || echo "Nginx配置检查失败"

# 7. 创建systemd服务
echo "⚙️  配置系统服务..."
cat > /etc/systemd/system/paodinglaw.service << 'UNITEOF'
[Unit]
Description=庖丁法律服务
After=network.target mysql.service

[Service]
Type=simple
User=root
WorkingDirectory=/var/www/paodinglaw
Environment=MYSQL_HOST=127.0.0.1
Environment=MYSQL_PORT=3306
Environment=MYSQL_USER=root
Environment=MYSQL_PASSWORD=Bos123mvc!
Environment=MYSQL_DATABASE=paoding_law
ExecStart=/usr/local/python310/bin/python3 -m gunicorn --workers 4 --worker-class eventlet --bind 127.0.0.1:8000 --pid /tmp/gunicorn.pid app:app --access-logfile /var/www/paodinglaw/logs/gunicorn_access.log --error-logfile /var/www/paodinglaw/logs/gunicorn_error.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable paodinglaw
systemctl restart paodinglaw

echo ""
echo "✅ 部署完成!"
echo "   地址: http://$DOMAIN"
echo "   管理员: admin / admin123"
