#!/usr/bin/env bash
# Вход в МЭШ (школьный дневник) на сервере. Запуск: sudo bash deploy/mes_login.sh [--tokens]
#   без аргументов — QR-вход (лучше всего: у сервера своя сессия);
#   --tokens — вставить aupd_token и aupd_refresh_token из окна инкогнито (окно потом закрыть, не нажимая «Выйти»).
# Сервис перезапускать не нужно: он сам подхватит новые токены.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }
set -a; . /etc/tg-mcp/env; set +a
cd /opt/tg-mcp
runuser -u tgmcp -- /opt/tg-mcp/venv/bin/python /opt/tg-mcp/mes_login.py "$@"
