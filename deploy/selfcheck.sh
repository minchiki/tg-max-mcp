#!/usr/bin/env bash
# Проверка живых подключений (МЭШ, Умскул). Запуск: sudo bash deploy/selfcheck.sh. Ежедневно её же запускает таймер tg-mcp-selfcheck.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }
set -a; . /etc/tg-mcp/env; set +a
cd /opt/tg-mcp
exec runuser -u tgmcp -- env MES_FILE=/var/lib/tg-mcp/mes.json /opt/tg-mcp/venv/bin/python /opt/tg-mcp/selfcheck.py
