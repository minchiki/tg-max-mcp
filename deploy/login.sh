#!/usr/bin/env bash
# Одноразовый вход в Telegram по QR на сервере. Запуск: sudo bash deploy/login.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }
set -a; . /etc/tg-mcp/env; set +a
systemctl stop tg-mcp 2>/dev/null || true   # сессия — sqlite, не открываем её из двух процессов
cd /opt/tg-mcp
runuser -u tgmcp -- /opt/tg-mcp/venv/bin/python /opt/tg-mcp/login_qr.py
systemctl is-enabled --quiet tg-mcp 2>/dev/null && systemctl start tg-mcp || true
