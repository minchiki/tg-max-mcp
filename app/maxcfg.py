"""Общие настройки MAX (web.max.ru) через PyMax: сессия рядом с Telegram-сессией."""
import os

from pymax import ExtraConfig, WebClient

MAX_DIR = os.environ.get("MAX_DIR", os.path.dirname(os.environ.get("TG_SESSION", ".")) or ".")
MAX_SESSION = "max.db"


def session_exists() -> bool:
    return os.path.exists(os.path.join(MAX_DIR, MAX_SESSION))


class QuietWebClient(WebClient):
    """Постоянное подключение не должно держать аккаунт «в сети»."""

    async def _prepare_config(self):
        config = await super()._prepare_config()
        config.interactive = False
        return config


def make_client(**kw) -> WebClient:
    return QuietWebClient(
        session_name=MAX_SESSION,
        work_dir=MAX_DIR,
        extra_config=ExtraConfig(reconnect=False, relogin=False, telemetry=False, log_level="WARNING"),
        **kw,
    )
