#!/usr/bin/env bash
# Вход в Умскул на сервере. Запуск: sudo bash deploy/umschool_login.sh
# Спросит почту и пароль (пароль не показывается и нигде не сохраняется, на диск идут только токены).
# Сервис перезапускать не нужно: он сам подхватит вход.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }
set -a; . /etc/tg-mcp/env; set +a
cd /opt/tg-mcp
runuser -u tgmcp -- /opt/tg-mcp/venv/bin/python /opt/tg-mcp/umschool_login.py "$@"
