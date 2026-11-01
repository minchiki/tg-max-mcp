#!/usr/bin/env bash
# Установка tg-max-mcp на VPS (Debian/Ubuntu). Запуск из корня репозитория: sudo bash deploy/install.sh
# Свой пользователь, свой каталог, свой порт на 127.0.0.1 — другие сервисы на сервере не затрагиваются.
# Повторный запуск обновляет код и зависимости, настройки и сессии сохраняются.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/tg-mcp
ENVF=/etc/tg-mcp/env

apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl openssl

id tgmcp >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin tgmcp
mkdir -p "$APP" /etc/tg-mcp /var/lib/tg-mcp
cp "$ROOT"/app/*.py "$APP"/
chown -R root:root "$APP"
chown tgmcp:tgmcp /var/lib/tg-mcp && chmod 700 /var/lib/tg-mcp

[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q -U pip
"$APP/venv/bin/pip" install -q -r "$ROOT/requirements.txt"

if [ ! -f "$ENVF" ]; then
  cp "$ROOT/deploy/env.example" "$ENVF"
  echo "TG_SECRET=$(openssl rand -hex 24)" >> "$ENVF"
  NEW_ENV=1
fi
chown root:tgmcp "$ENVF" && chmod 640 "$ENVF"

cp "$ROOT/deploy/tg-mcp.service" /etc/systemd/system/tg-mcp.service
systemctl daemon-reload
systemctl is-active --quiet tg-mcp && systemctl restart tg-mcp

if [ -n "${NEW_ENV:-}" ]; then
  echo
  echo ">>> Впишите TG_API_ID и TG_API_HASH (https://my.telegram.org) в $ENVF:  sudo nano $ENVF"
fi
cat <<'EOF'

Дальше по порядку:
  1) sudo bash deploy/setup-vpn.sh "<подписка или vless://...>" [сервер]   (только если Telegram заблокирован у хостера)
  2) sudo bash deploy/login.sh          — вход в Telegram по QR
     sudo bash deploy/max_login.sh      — вход в MAX по QR (необязательно)
  3) sudo systemctl enable --now tg-mcp
  4) sudo bash deploy/setup-https.sh [домен]   — HTTPS-адрес для ChatGPT / Claude
EOF
