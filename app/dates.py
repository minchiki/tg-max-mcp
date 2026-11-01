"""Даты для инструментов истории: ISO, ДД.ММ.ГГГГ, месяц и год целиком, «год назад», «6 мес», «30д»."""
import calendar
import re
from datetime import datetime, timedelta

_FORMATS = (
    ("%Y-%m-%d %H:%M:%S", None), ("%Y-%m-%dT%H:%M:%S", None), ("%Y-%m-%d %H:%M", None), ("%Y-%m-%dT%H:%M", None),
    ("%d.%m.%Y %H:%M", None), ("%Y-%m-%d", "day"), ("%d.%m.%Y", "day"), ("%d.%m.%y", "day"),
    ("%Y-%m", "month"), ("%m.%Y", "month"), ("%Y", "year"),
)
_WORDS = {"вчера": timedelta(days=1), "неделю назад": timedelta(days=7), "месяц назад": timedelta(days=30),
          "полгода назад": timedelta(days=182), "год назад": timedelta(days=365), "два года назад": timedelta(days=730)}
_REL = re.compile(r"^\s*-?(\d+)\s*(лет|года|год|г|y|мес|м|m|нед|н|w|дней|дня|день|д|d|часов|часа|час|ч|h)\b.*$", re.I)
_UNIT = {"y": 365, "г": 365, "год": 365, "года": 365, "лет": 365, "m": 30, "м": 30, "мес": 30,
         "w": 7, "н": 7, "нед": 7, "d": 1, "д": 1, "день": 1, "дня": 1, "дней": 1}
_HOURS = {"h", "ч", "час", "часа", "часов"}

HELP = "формат: 2025-11-01, 2025-11-01 14:30, 31.10.2025, 2025-03 (месяц), 2025 (год), 1y / 6m / 30d / 12h, «год назад»"


def parse_when(s, tz, end=False):
    """Строка → aware datetime в часовом поясе tz. end=True: конец периода (для date_to «2025-11-01» это 23:59:59)."""
    s = (s or "").strip()
    if not s:
        return None
    low = s.lower()
    now = datetime.now(tz)
    if low in ("сейчас", "now", "сегодня", "today"):
        return now if low in ("сейчас", "now") or end else now.replace(hour=0, minute=0, second=0, microsecond=0)
    if low in _WORDS:
        return now - _WORDS[low]
    m = _REL.match(low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - (timedelta(hours=n) if unit in _HOURS else timedelta(days=n * _UNIT[unit]))
    for fmt, whole in _FORMATS:
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if end and whole == "day":
            d = d.replace(hour=23, minute=59, second=59)
        elif whole == "month":
            d = d.replace(day=calendar.monthrange(d.year, d.month)[1], hour=23, minute=59, second=59) if end else d
        elif whole == "year":
            d = d.replace(month=12, day=31, hour=23, minute=59, second=59) if end else d
        return d.replace(tzinfo=tz)
    raise ValueError(f"Не понял дату «{s}»: {HELP}")
