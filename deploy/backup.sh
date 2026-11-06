#!/usr/bin/env bash
# Резервная копия /var/lib/tg-mcp (сессии Telegram и MAX, токены МЭШ и Умскула), зашифрованная AES-256.
# Запуск: sudo bash deploy/backup.sh. Ежедневно её же запускает таймер tg-mcp-backup. Хранится 7 последних копий.
# Ключ лежит в /etc/tg-mcp/backup.key: сохраните его отдельно (менеджер паролей), без ключа копию не открыть.
# Восстановление: sudo bash deploy/backup.sh --restore /var/backups/tg-mcp/<файл>.tar.gz.enc
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }
DIR=/var/backups/tg-mcp; KEY=/etc/tg-mcp/backup.key; SRC=/var/lib/tg-mcp
umask 077
mkdir -p "$DIR"
[ -f "$KEY" ] || { openssl rand -base64 32 > "$KEY"; chmod 600 "$KEY"; echo "Создан ключ $KEY: сохраните его в надёжном месте"; }
if [ "${1:-}" = "--restore" ]; then
  F="${2:?укажите файл копии}"
  systemctl stop tg-mcp
  openssl enc -d -aes-256-cbc -pbkdf2 -pass "file:$KEY" -in "$F" | tar -xz -C /
  chown -R tgmcp:tgmcp "$SRC"; chmod 700 "$SRC"
  systemctl start tg-mcp
  echo "Восстановлено из $F"; exit 0
fi
OUT="$DIR/tg-mcp-$(date +%Y%m%d-%H%M%S).tar.gz.enc"
# sqlite-сессии могут писаться в момент копирования: снимок берём из tar, у Telethon это журналируемая база
tar -cz -C / "${SRC#/}" | openssl enc -aes-256-cbc -pbkdf2 -pass "file:$KEY" -out "$OUT"
ls -1t "$DIR"/tg-mcp-*.tar.gz.enc | tail -n +8 | xargs -r rm -f
echo "Копия: $OUT ($(du -h "$OUT" | cut -f1))"
