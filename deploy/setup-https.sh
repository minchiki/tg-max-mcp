#!/usr/bin/env bash
# HTTPS-адрес для ChatGPT через уже установленный nginx + бесплатный сертификат Let's Encrypt.
# Запуск: sudo bash setup-https.sh [домен]   (без аргумента берётся <ip>.sslip.io)
# Отдельный server-блок только для этого имени: сайт MC Panel не затрагивается.
# Нужны открытые порты 80 и 443 (security group облака).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }
set -a; . /etc/tg-mcp/env; set +a

HOST="${1:-}"
if [ -z "$HOST" ]; then
  IP="$(curl -fsS -m 10 https://api.ipify.org || hostname -I | awk '{print $1}')"
  HOST="${IP//./-}.sslip.io"
fi

command -v nginx >/dev/null || apt-get install -y nginx
apt-get install -y certbot python3-certbot-nginx

cat > /etc/nginx/sites-available/tg-mcp <<EOF
server {
    listen 80;
    server_name $HOST;
    location /$TG_SECRET/mcp {
        proxy_pass http://127.0.0.1:${TG_PORT:-8765};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
    location / { return 404; }
}
EOF
ln -sf /etc/nginx/sites-available/tg-mcp /etc/nginx/sites-enabled/tg-mcp
nginx -t
systemctl reload nginx

certbot --nginx -d "$HOST" --non-interactive --agree-tos --register-unsafely-without-email --redirect
nginx -t && systemctl reload nginx

echo
echo "Адрес для ChatGPT (держите в секрете):"
echo "https://$HOST/$TG_SECRET/mcp"
