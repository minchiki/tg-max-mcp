"""Общая конфигурация: ключи, сессия, прокси. Всё берётся из переменных окружения."""
import os
from urllib.parse import urlparse

from telethon import TelegramClient

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.path.expanduser(os.environ.get("TG_SESSION", "~/.tg_claude"))


def proxy():
    """TG_PROXY=socks5://[user:pass@]host:port (или http://...). Пусто = без прокси."""
    u = os.environ.get("TG_PROXY", "").strip()
    if not u:
        return None
    p = urlparse(u)
    d = {"proxy_type": p.scheme.replace("socks5h", "socks5"),
         "addr": p.hostname, "port": p.port, "rdns": True}
    if p.username:
        d["username"] = p.username
        d["password"] = p.password or ""
    return d


def make_client():
    return TelegramClient(SESSION, API_ID, API_HASH, proxy=proxy())
