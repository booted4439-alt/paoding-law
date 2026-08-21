#!/bin/bash
# ============================================================
# 庖丁法律服务 (calculuslaw.com) — 生产部署脚本 v2
#
# v2 (2026-08-21) 修正内容：
#   1. Python 路径: /usr/local/miniconda3/bin/python3
#      (原 /usr/local/python310/bin/python3 在服务器上已不存在，旧脚本必挂)
#   2. 数据库:      paodinglaw / paodinglaw_user
#      (原 paoding_law / root 是错误配置，root 账号在 MySQL 中无法登录)
#   3. Nginx:       写入 HTTPS 配置（原脚本写入 HTTP 配置，会覆盖证书站点导致全站变 http）
#   4. systemd:     不再覆盖已存在的服务文件
#      (原脚本重写服务文件会丢失短信/微信/支付宝的环境变量)
#   5. Git:         禁止 reset --hard，改用 fast-forward + 脏工作区保护
#      (reset --hard 会抹掉服务器上的本地修改，如手工改的代码)
#   6. 新增:        部署前自动备份数据库
#
# 用法: bash deploy.sh
# ============================================================
set -e

APP_DIR="/var/www/paodinglaw"
PYTHON="/usr/local/miniconda3/bin/python3"
NGINX_CONF="/etc/nginx/conf.d/calculuslaw.com.conf"
DOMAIN="calculuslaw.com"
SERVICE_NAME="paodinglaw"

# ---- MySQL（必须与 systemd 服务中的 MYSQL_* 保持一致）----
DB_HOST="127.0.0.1"
DB_PORT="3306"
DB_USER="paodinglaw_user"
DB_PASS="Paoding123!123"
DB_NAME="paodinglaw"
# root 密码仅用于首次建库/建用户；无 root 权限时留空即可（自动跳过）
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-}"

BACKUP_DIR="/root/deploy_backups/$(date +%Y%m%d_%H%M%S)"

echo "🔪 部署庖丁法律服务 ($DOMAIN) ..."
cd "$APP_DIR"

# 1. 备份数据库
mkdir -p "$BACKUP_DIR"
if mysqldump -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" > "$BACKUP_DIR/${DB_NAME}.sql" 2>/dev/null; then
    echo "✅ 数据库已备份: $BACKUP_DIR/${DB_NAME}.sql"
else
    echo "⚠️ 数据库备份失败，继续部署（确认 MySQL 正常时该提示可忽略）"
fi

# 2. 拉取最新代码（安全模式：绝不 reset --hard）
echo "🔧 拉取最新代码..."
git fetch origin
if [ -n "$(git status --porcelain)" ]; then
    echo "❌ 工作区存在未提交的本地修改，部署中止！请先处理："
    git status --short
    exit 1
fi
git merge --ff-only origin/main
echo "✅ 代码已更新至: $(git log --oneline -1)"

# 3. 安装依赖
echo "📦 安装 Python 依赖..."
"$PYTHON" -m pip install --upgrade pip --quiet
"$PYTHON" -m pip install -r requirements.txt --quiet
"$PYTHON" -m pip install gunicorn --quiet

# 4. 数据库：首次创建库/用户（尽力而为；库已存在或没有 root 权限时自动跳过）
if [ -n "$MYSQL_ROOT_PASSWORD" ]; then
    mysql -h "$DB_HOST" -u root -p"$MYSQL_ROOT_PASSWORD" -e \
        "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
         CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
         GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
         FLUSH PRIVILEGES;" 2>/dev/null \
        && echo "✅ 数据库/用户就绪" \
        || echo "⚠️ 无法用 root 创建数据库（可能无权限），跳过；表结构由下一步创建"
else
    echo "ℹ️  MYSQL_ROOT_PASSWORD 为空，跳过建库/建用户（仅首次部署时需要）"
fi

# 5. 初始化表结构（幂等：只创建缺失的表，不会删表）
export MYSQL_HOST="$DB_HOST" MYSQL_PORT="$DB_PORT" MYSQL_USER="$DB_USER" MYSQL_PASSWORD="$DB_PASS" MYSQL_DATABASE="$DB_NAME"
export FLASK_ENV=production
"$PYTHON" -c "from app import app, db; app.app_context().push(); db.create_all(); print('✅ 数据库表结构就绪')"

# 6. Nginx 配置（HTTPS 版本，勿回退为 HTTP）
echo "🔧 写入 Nginx 配置..."
cat > "$NGINX_CONF" << 'NGINXEOF'
# HTTP → HTTPS 跳转
server {
    listen 80;
    server_name calculuslaw.com www.calculuslaw.com;
    return 301 https://$server_name$request_uri;
}

# HTTPS
server {
    listen 443 ssl http2;
    server_name calculuslaw.com www.calculuslaw.com;

    ssl_certificate     /etc/nginx/ssl/www.calculuslaw.com.pem;
    ssl_certificate_key /etc/nginx/ssl/www.calculuslaw.com.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    client_max_body_size 500M;

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

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /admin {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /auth/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /consult {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /documents {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /uploads/ {
        alias /var/www/paodinglaw/static/uploads/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location /static/ {
        alias /var/www/paodinglaw/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    access_log /var/log/nginx/calculuslaw_access.log;
    error_log /var/log/nginx/calculuslaw_error.log;
}
NGINXEOF
if nginx -t; then
    systemctl reload nginx
    echo "✅ Nginx 已重载"
else
    echo "❌ Nginx 配置检查失败，请手动检查 $NGINX_CONF"
    exit 1
fi

# 7. systemd 服务（已存在则保留现有配置，避免丢失短信/微信/支付宝环境变量）
if [ -f "/etc/systemd/system/$SERVICE_NAME.service" ]; then
    echo "✅ systemd 服务已存在，保留现有配置（含短信/微信/支付宝环境变量）"
else
    echo "⚙️  首次创建 systemd 服务..."
    if [ -f "$APP_DIR/.env" ]; then
        set -a; . "$APP_DIR/.env"; set +a
    fi
    cat > "/etc/systemd/system/$SERVICE_NAME.service" << EOF
[Unit]
Description=庖丁法律服务
After=network.target mysqld.service

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=MYSQL_HOST=$DB_HOST
Environment=MYSQL_PORT=$DB_PORT
Environment=MYSQL_USER=$DB_USER
Environment=MYSQL_PASSWORD=$DB_PASS
Environment=MYSQL_DATABASE=$DB_NAME
Environment=ALIYUN_SMS_AK=$ALIYUN_SMS_AK
Environment=ALIYUN_SMS_SK=$ALIYUN_SMS_SK
Environment=ALIYUN_SMS_SIGN=$ALIYUN_SMS_SIGN
Environment=ALIYUN_SMS_TPL=$ALIYUN_SMS_TPL
Environment=WX_MINI_APPID=$WX_MINI_APPID
Environment=WX_MINI_SECRET=$WX_MINI_SECRET
Environment=WX_WEB_APPID=$WX_WEB_APPID
Environment=WX_WEB_SECRET=$WX_WEB_SECRET
Environment=SMTP_USER=$SMTP_USER
Environment=SMTP_PASS=$SMTP_PASS
Environment=ALIPAY_APP_ID=$ALIPAY_APP_ID
Environment=ALIPAY_PRIVATE_KEY_PATH=$APP_DIR/alipay_private.pem
Environment=ALIPAY_PUBLIC_KEY_PATH=$APP_DIR/alipay_public.pem
ExecStart=$PYTHON -m gunicorn --workers 2 --timeout 120 --bind 127.0.0.1:8000 --pid /tmp/gunicorn.pid --access-logfile $APP_DIR/logs/gunicorn_access.log --error-logfile $APP_DIR/logs/gunicorn_error.log app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" 2>/dev/null || true
systemctl restart "$SERVICE_NAME"
echo "✅ 服务已重启"

echo ""
echo "✅ 部署完成! 地址: https://$DOMAIN"
