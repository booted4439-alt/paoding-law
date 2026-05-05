#!/bin/bash
# 本地开发服务器启动脚本
set -a
source "$(dirname "$0")/.env"
set +a
cd "$(dirname "$0")"
exec python3 app.py "$@"
