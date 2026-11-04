"""Уведомления из МЭШ в Telegram через бота: сервер сам пишет, когда это важно.

  • новая оценка, новое ДЗ, изменение расписания на сегодня и завтра (опрос раз в MES_NOTIFY_EVERY_MIN, по умолчанию 20 минут);
  • утренняя сводка в будни (час MES_DIGEST_HOUR, по умолчанию 7): уроки, несделанное ДЗ, оценки, питание;
  • воскресная сводка в 18:00: средние по предметам и ДЗ на неделю;
  • сообщение, если вход в МЭШ слетел (и когда восстановился).

Включается, когда заданы MES_BOT_TOKEN (токен бота от @BotFather) и MES_BOT_CHAT (ваш числовой id в Telegram).
Сообщения уходят только в этот чат. Первый опрос ничего не присылает, он запоминает текущее состояние.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

import httpx

import mes
from safety import clean

EVERY = max(5, int(os.environ.get("MES_NOTIFY_EVERY_MIN", "20") or 20)) * 60
DIGEST_HOUR = int(os.environ.get("MES_DIGEST_HOUR", "7") or 7)
SUNDAY_HOUR = 18
FILE = os.environ.get("MES_NOTIFY_FILE") or os.path.join(os.path.dirname(mes.FILE), "mes_notify.json")
st = {}
_task = None


def token():
    return os.environ.get("MES_BOT_TOKEN", "").strip()


def chat():
    return os.environ.get("MES_BOT_CHAT", "").strip()


def enabled():
    return os.environ.get("MES_NOTIFY", "1") != "0" and bool(token()) and chat().lstrip("-").isdigit()


def log(msg):
    print(f"mesnotify: {msg}", file=sys.stderr, flush=True)


def load():
    st.clear()
    try:
        with open(FILE, encoding="utf-8") as f:
            st.update(json.load(f))
    except (OSError, ValueError):
        pass


def save():
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, FILE)


def _proxy():
    return (os.environ.get("MES_BOT_PROXY") or os.environ.get("TG_PROXY") or "").strip().replace("socks5h://", "socks5://") or None


async def send(text):
    """Отправить сообщение боту-получателю. Токен в адресе: исключения httpx в журнал не пишем, только их тип."""
    try:
        async with httpx.AsyncClient(proxy=_proxy(), timeout=20) as h:
            r = await h.post(f"https://api.telegram.org/bot{token()}/sendMessage",
                             json={"chat_id": int(chat()), "text": text[:4000], "disable_web_page_preview": True})
        if r.status_code != 200:
            log(f"Telegram ответил {r.status_code}")
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log(f"не отправил ({type(e).__name__})")
        return False


# ---------- данные ----------

async def _marks(p, a, b):
    d = await mes.get("family/web/v1/marks", {"from": a.isoformat(), "to": b.isoformat(), "student_id": p["student_id"]},
                      profile_id=p["profile_id"], ttl=0)
    return (d.get("payload") if isinstance(d, dict) else d) or []


async def _homework(p, a, b):
    d = await mes.get("family/web/v1/homeworks", {"from": a.isoformat(), "to": b.isoformat(), "student_id": p["student_id"]},
                      profile_id=p["profile_id"], ttl=0)
    return (d.get("payload") if isinstance(d, dict) else d) or []


async def _events(p, a, b):
    d = await mes.get("eventcalendar/v1/api/events", {"person_ids": p["person_id"], "begin_date": a.isoformat(), "end_date": b.isoformat(),
                                                      "expand": "marks,homework"}, profile_id=p["profile_id"], role="student", ttl=0)
    return (d.get("response") if isinstance(d, dict) else d) or []


def lessons(events, day):
    """Строки расписания на день: «08:30 Алгебра, каб. 301 (ОТМЕНЕНО)». Для сравнения и для сводки."""
    out = []
    for e in sorted(events, key=lambda x: x.get("start_at") or ""):
        if (e.get("start_at") or "")[:10] != day or not (e.get("subject_name") or e.get("title")):
            continue
        s = f"{(e.get('start_at') or '')[11:16]} {clean(e.get('subject_name') or e.get('title'), 50)}"
        if e.get("room_number") or e.get("room_name"):
            s += f", каб. {clean(e.get('room_number') or e.get('room_name'), 15)}"
        if e.get("cancelled") or e.get("is_cancelled"):
            s += " (ОТМЕНЕНО)"
        elif e.get("replaced") or e.get("is_replaced"):
            s += " (замена)"
        out.append(s)
    return out


def mark_line(m):
    return f"{clean(m.get('subject_name'), 40)}: {m.get('value')}" + (f" ({clean(m.get('control_form_name'), 40)})" if m.get("control_form_name") else "")


def mark_key(m):
    return str(m.get("id") or f"{m.get('date')}|{m.get('subject_name')}|{m.get('value')}|{m.get('created_at') or m.get('comment') or ''}")


def hw_line(h):
    return f"{clean(h.get('subject_name'), 40)} на {h.get('date')}: {clean(h.get('description') or '(без текста)', 250)}"


# ---------- проверки ----------

async def check(now=None):
    """Один опрос: новые оценки / ДЗ / изменения расписания и состояние входа. Возвращает список отправленных сообщений."""
    now = now or datetime.now(mes.TZ)
    today = now.date()
    sent = []
    try:
        p = await mes.me()
        marks = await _marks(p, today - timedelta(days=10), today)
        hws = await _homework(p, today, today + timedelta(days=10))
        evs = await _events(p, today, today + timedelta(days=1))
    except mes.MesAuthError as e:
        if not st.get("auth_alert"):
            text = f"МЭШ: вход слетел, сервер больше не видит дневник ({clean(str(e), 150)}). Войдите заново QR-кодом: sudo bash deploy/mes_login.sh"
            if await send(text):
                st["auth_alert"] = True
                save()
                sent.append(text)
        return sent
    except mes.MesError as e:
        log(f"МЭШ недоступен: {clean(str(e), 150)}")
        return sent

    if st.get("auth_alert"):
        if await send("МЭШ: вход восстановлен, дневник снова доступен."):
            st["auth_alert"] = False
        sent.append("restored")

    seen_m, seen_h = set(st.get("marks") or []), set(st.get("hw") or [])
    cur_m = {mark_key(m): m for m in marks}
    cur_h = {str(h.get("homework_entry_id") or h.get("homework_id") or hw_line(h)): h for h in hws}
    sched_now = {d.isoformat(): lessons(evs, d.isoformat()) for d in (today, today + timedelta(days=1))}
    old_sched = st.get("sched") or {}

    lines = []
    if st.get("init"):
        for k, m in cur_m.items():
            if k not in seen_m:
                lines.append("Новая оценка: " + mark_line(m))
        for k, h in cur_h.items():
            if k not in seen_h:
                lines.append("Новое ДЗ: " + hw_line(h))
        for day, new in sched_now.items():
            old = old_sched.get(day)
            if old is not None and old != new and new:
                changed = [x for x in new if x not in old]
                gone = [x for x in old if x not in new]
                lines.append(f"Расписание на {day} изменилось:" + ("".join(f"\n  + {x}" for x in changed)) + ("".join(f"\n  − {x}" for x in gone)))
    if not lines or await send("\n".join(lines)):
        st.update(init=True, marks=sorted(set(cur_m) | seen_m)[-400:], hw=sorted(set(cur_h) | seen_h)[-400:],
                  sched={**{k: v for k, v in old_sched.items() if k >= today.isoformat()}, **sched_now})
        save()
        if lines:
            sent.append("\n".join(lines))
    return sent


async def digest(now=None, force=False):
    """Утренняя сводка в будни и воскресный итог. Раз в день, не позже полудня (воскресенье: вечером)."""
    now = now or datetime.now(mes.TZ)
    today = now.date()
    wd = now.weekday()
    sunday = wd == 6
    due = (now.hour >= SUNDAY_HOUR) if sunday else (DIGEST_HOUR <= now.hour < 12 and wd < 5)
    if not (due or force) or (st.get("digest_day") == today.isoformat() and not force):
        return None
    p = await mes.me()
    parts = []
    if sunday:
        data = await mes.get("family/web/v1/subject_marks", {"student_id": p["student_id"]}, profile_id=p["profile_id"], ttl=0)
        items = (data.get("payload") if isinstance(data, dict) else data) or []
        parts.append("Итоги недели по предметам:\n" + (mes.fmt_subject_marks(items) or "данных нет"))
        hws = await _homework(p, today + timedelta(days=1), today + timedelta(days=7))
        parts.append("ДЗ на неделю:\n" + ("\n".join(f"• {hw_line(h)}" for h in hws if not h.get("is_done")) or "всё сделано или не задано"))
    else:
        evs = await _events(p, today, today)
        ls = lessons(evs, today.isoformat())
        parts.append(f"Доброе утро. Сегодня {today.strftime('%d.%m')}:\n" + ("\n".join(ls) if ls else "уроков нет"))
        hws = await _homework(p, today, today + timedelta(days=3))
        undone = [h for h in hws if not h.get("is_done")]
        parts.append("Несделанное ДЗ:\n" + ("\n".join(f"• {hw_line(h)}" for h in undone) if undone else "всё сделано"))
        ms = await _marks(p, today - timedelta(days=2), today)
        if ms:
            parts.append("Оценки за последние дни:\n" + "\n".join(f"• {m.get('date')} {mark_line(m)}" for m in ms))
        try:
            bal = await mes.food("food/meals/v3/clients/balance", p)
            menu = await mes.food("food/meals/v3/menu/complexes", p, **{"from": today.isoformat(), "to": today.isoformat(), "withPreorder": "in"})
            names = [f"{c.get('name')} {mes.rub(c.get('price'))}" for d in menu or [] for c in d.get("items") or []]
            parts.append(f"Питание: на карте {mes.rub(bal[0].get('balance')) if bal else '?'}" + (f"; в меню: {', '.join(names)}" if names else ""))
        except mes.MesError as e:
            log(f"питание в сводке пропущено: {clean(str(e), 100)}")
    text = "\n\n".join(parts)
    if await send(text):
        if not force:
            st["digest_day"] = today.isoformat()
            save()
        return text
    return None


async def evening(now=None):
    """Если включено в настройках бота: в 18:00 присылает ДЗ на завтра (раз в день)."""
    now = now or datetime.now(mes.TZ)
    tomorrow = now.date() + timedelta(days=1)
    if not st.get("hw_evening") or now.hour < 18 or st.get("evening_day") == now.date().isoformat():
        return None
    p = await mes.me()
    hws = [h for h in await _homework(p, tomorrow, tomorrow) if not h.get("is_done")]
    if hws:
        text = f"ДЗ на завтра, {tomorrow.strftime('%d.%m')}:\n" + "\n".join(f"• {hw_line(h)}" for h in hws)
        if not await send(text):
            return None
    st["evening_day"] = now.date().isoformat()
    save()
    return hws


async def tick():
    if not mes.logged_in():
        return
    if not st.get("notify_off"):
        await check()
    if not st.get("digest_off"):
        await digest()
    await evening()


async def loop():
    await asyncio.sleep(45)
    while True:
        try:
            await tick()
        except Exception as e:  # noqa: BLE001
            log(f"ошибка цикла ({type(e).__name__}: {clean(str(e), 120)})")
        await asyncio.sleep(EVERY)


def start():
    """Запускается при старте сервера. Без MES_BOT_TOKEN / MES_BOT_CHAT ничего не делает."""
    global _task
    if not enabled():
        return
    load()
    mes.load()
    _task = asyncio.get_running_loop().create_task(loop())
    log(f"включены уведомления в чат {chat()}, опрос раз в {EVERY // 60} мин")
    import mesbot
    mesbot.start()
