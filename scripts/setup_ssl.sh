#!/bin/bash
# SSL 证书自动配置脚本
#
# 用法:
#   sudo bash scripts/setup_ssl.sh your-domain.com [your-email@example.com]
#
# 功能:
#   1. 安装 certbot + nginx 插件
#   2. 获取 Let's Encrypt 证书
#   3. 部署 SSL nginx 配置
#   4. 配置自动续期
#
# 前提:
#   - 域名 DNS 已指向本机 IP
#   - 80 端口可被外部访问（Let's Encrypt 验证用）
#   - nginx 已安装

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"

if [ -z "$DOMAIN" ]; then
    echo "用法: sudo bash $0 <domain> [email]"
    echo "例如: sudo bash $0 bot.example.com admin@example.com"
    exit 1
fi

echo "=== SSL 证书配置开始 ==="
echo "域名: $DOMAIN"

# 1. 安装 certbot
echo ""
echo "--- 1/5 安装 certbot ---"
if ! command -v certbot &> /dev/null; then
    apt-get update -qq
    apt-get install -y -qq certbot python3-certbot-nginx
    echo "certbot 安装完成"
else
    echo "certbot 已安装"
fi

# 2. 确保 nginx 运行
echo ""
echo "--- 2/5 检查 nginx ---"
if ! command -v nginx &> /dev/null; then
    echo "错误: nginx 未安装，请先运行: sudo apt install nginx"
    exit 1
fi
nginx -t 2>/dev/null || true
systemctl start nginx 2>/dev/null || nginx

# 3. 创建验证目录
echo ""
echo "--- 3/5 准备验证目录 ---"
mkdir -p /var/www/certbot

# 4. 获取证书
echo ""
echo "--- 4/5 获取 SSL 证书 ---"
CERTBOT_ARGS="certonly --nginx -d $DOMAIN --non-interactive --agree-tos"
if [ -n "$EMAIL" ]; then
    CERTBOT_ARGS="$CERTBOT_ARGS --email $EMAIL"
else
    CERTBOT_ARGS="$CERTBOT_ARGS --register-unsafely-without-email"
fi

certbot $CERTBOT_ARGS

# 5. 部署 SSL nginx 配置
echo ""
echo "--- 5/5 部署 nginx SSL 配置 ---"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$SCRIPT_DIR/../templates/nginx-site-ssl.conf"

if [ -f "$TEMPLATE" ]; then
    # 替换域名
    sed "s/YOUR_DOMAIN/$DOMAIN/g" "$TEMPLATE" > /etc/nginx/conf.d/bot-proxy.conf
    mkdir -p /etc/nginx/conf.d/tenants

    # 测试并重载
    nginx -t
    nginx -s reload

    echo ""
    echo "=== SSL 配置完成 ==="
    echo "HTTPS: https://$DOMAIN"
    echo "证书路径: /etc/letsencrypt/live/$DOMAIN/"
    echo "nginx 配置: /etc/nginx/conf.d/bot-proxy.conf"
    echo ""
    echo "自动续期已由 certbot 配置（systemd timer）"
    echo "测试续期: sudo certbot renew --dry-run"
else
    echo "警告: 未找到 SSL 模板 ($TEMPLATE)"
    echo "证书已获取，请手动配置 nginx"
    echo "证书路径: /etc/letsencrypt/live/$DOMAIN/"
fi
