#!/usr/bin/env bash
# Ставит xray и поднимает SOCKS5 на 127.0.0.1:1080 из вашей подписки.
# Только для Telegram MCP: весь остальной трафик сервера (MC Panel и т.д.) не затрагивается.
# Запуск: sudo bash setup-vpn.sh "<URL подписки | vless://...>" [номер сервера из списка]
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }
SUB="${1:?Укажите ссылку подписки или vless://}"
IDX="${2:-0}"
DIR="$(cd "$(dirname "$0")" && pwd)"

if ! command -v xray >/dev/null; then
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
fi

python3 "$DIR/vless2xray.py" "$SUB" "$IDX" > /usr/local/etc/xray/config.json
# группа, от имени которой работает сервис xray (xray / nogroup, зависит от версии установщика)
XGRP="$(systemctl show xray -p Group --value 2>/dev/null || true)"
[ -n "$XGRP" ] && getent group "$XGRP" >/dev/null || XGRP="$(getent group xray >/dev/null && echo xray || echo nogroup)"
chown root:"$XGRP" /usr/local/etc/xray/config.json
chmod 640 /usr/local/etc/xray/config.json
systemctl enable xray
systemctl restart xray
sleep 3

code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' --socks5-hostname 127.0.0.1:1080 https://api.telegram.org || true)
echo "Telegram через VPN: HTTP $code (302/404/200 = достучались, 000 = нет)"

grep -q '^TG_PROXY=' /etc/tg-mcp/env || echo 'TG_PROXY=socks5://127.0.0.1:1080' >> /etc/tg-mcp/env
echo "TG_PROXY прописан в /etc/tg-mcp/env"
