"""Telegram-бот «Дневник МЭШ»: кнопки Сегодня / Завтра / Неделя / Расписание / ЦДЗ / Оценки / Решить ДЗ / Еда / Настройки.

Работает на том же токене (MES_BOT_TOKEN) и отвечает только в чате MES_BOT_CHAT. Опрос getUpdates, вебхук не нужен.
Премиум-эмодзи из набора NewsEmoji: на кнопках (icon_custom_emoji_id) и в тексте (<tg-emoji>). Если Telegram их не принял,
сообщение уходит обычными эмодзи.
"""
import asyncio
import html
import re
import os
import sys
from datetime import datetime, timedelta

import httpx

import mes
import mesnotify as nt
from safety import clean

# (id эмодзи из набора NewsEmoji, обычный запасной символ)
E = {
    "today": ("5413879192267805083", "🗓"), "tomorrow": ("5440621591387980068", "🔜"), "week": ("5231200819986047254", "📊"),
    "sched": ("5397782960512444700", "📌"), "cdz": ("5282843764451195532", "🖥"), "marks": ("5438496463044752972", "⭐"),
    "solve": ("5422439311196834318", "💡"), "food": ("5229064374403998351", "🛍"), "settings": ("5341715473882955310", "⚙️"),
    "done": ("5206607081334906820", "✔️"), "todo": ("5210952531676504517", "❌"), "pen": ("5395444784611480792", "✏️"),
    "link": ("5271604874419647061", "🔗"), "bell": ("5458603043203327669", "🔔"), "money": ("5409048419211682843", "💵"),
    "hot": ("5424972470023104089", "🔥"), "info": ("5334544901428229844", "ℹ️"), "gold": ("5440539497383087970", "🥇"),
    "warn": ("5447644880824181073", "⚠️"), "party": ("5461151367559141950", "🎉"), "clip": ("5305265301917549162", "📎"),
}
BUTTONS = [["today", "Сегодня", "tomorrow", "Завтра", "week", "Неделя"],
           ["sched", "Расписание", "cdz", "ЦДЗ", "marks", "Оценки"],
           ["solve", "Решить ДЗ", "food", "Еда", "settings", "Настройки"]]
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_task = None


def em(key):
    i, fb = E[key]
    return f'<tg-emoji emoji-id="{i}">{fb}</tg-emoji>'


def esc(s):
    return html.escape(str(s or ""), quote=False)


def plain(text):
    return re.sub(r"</?tg-emoji[^>]*>", "", text)


def log(msg):
    print(f"mesbot: {msg}", file=sys.stderr, flush=True)


async def api(method, **payload):
    """Вызов Bot API. Токен в адресе, поэтому в журнал идёт только тип ошибки."""
    try:
        async with httpx.AsyncClient(proxy=nt._proxy(), timeout=payload.pop("_timeout", 20)) as h:
            r = await h.post(f"https://api.telegram.org/bot{nt.token()}/{method}", json=payload)
        return r.status_code, r.json()
    except Exception as e:  # noqa: BLE001
        log(f"{method}: {type(e).__name__}")
        return 0, {}


def keyboard(icons=True):
    rows = []
    for row in BUTTONS:
        r = []
        for i in range(0, len(row), 2):
            b = {"text": row[i + 1]}
            if icons:
                b["icon_custom_emoji_id"] = E[row[i]][0]
            r.append(b)
        rows.append(r)
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": "Выберите действие"}


async def say(text, markup=None, edit=None):
    """Отправить (или заменить текст сообщения edit). Сначала с премиум-эмодзи, при отказе — с обычными."""
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for n, chunk in enumerate(chunks):
        last = n == len(chunks) - 1
        for attempt in (0, 1, 2):
            body = chunk if attempt == 0 else plain(chunk)
            payload = {"chat_id": int(nt.chat()), "text": body, "parse_mode": "HTML", "disable_web_page_preview": True}
            mk = markup if last else None
            if mk is not None:
                if attempt == 2 and "keyboard" in mk:
                    mk = keyboard(icons=False)
                payload["reply_markup"] = mk
            if edit and n == 0:
                payload["message_id"] = edit
                code, r = await api("editMessageText", **payload)
                if code == 400 and "not modified" in str(r.get("description", "")):
                    return
            else:
                code, r = await api("sendMessage", **payload)
            if code == 200:
                break
            log(f"{'edit' if edit else 'send'}: {code} {str(r.get('description', ''))[:80]}")
            if code == 0:
                break


# ---------- данные ----------

def day_title(d, today):
    rel = {0: " — сегодня", 1: " — завтра", -1: " — вчера"}.get((d - today).days, "")
    return f"{DAYS[d.weekday()]}, {d.strftime('%d.%m')}{rel}"


async def hw_for(a, b):
    p = await mes.me()
    return sorted(await nt._homework(p, a, b), key=lambda h: (h.get("date") or "", h.get("subject_name") or ""))


def hw_block(h):
    mark = em("done") if h.get("is_done") else em("pen")
    text = esc(clean(h.get("description") or "(без текста)", 300))
    extra = " " + em("clip") if (h.get("attachments") or mes.file_links(h)) else ""
    return f"{mark} <b>{esc(h.get('subject_name'))}</b>{extra}\n{text}"


def nav(prefix, d, mid_label="Сегодня", today=None):
    today = today or datetime.now(mes.TZ).date()
    return [[{"text": "◀", "callback_data": f"{prefix}:{(d - timedelta(days=1)).isoformat()}"},
             {"text": mid_label, "callback_data": f"{prefix}:{today.isoformat()}"},
             {"text": "▶", "callback_data": f"{prefix}:{(d + timedelta(days=1)).isoformat()}"}]]


async def view_hw(d):
    today = datetime.now(mes.TZ).date()
    items = await hw_for(d, d)
    cdz = sum(1 for h in items if h.get("materials"))
    done = sum(1 for h in items if h.get("is_done"))
    head = f"{em('today')} <b>{day_title(d, today)}</b>\nВсего: {len(items)} · {em('cdz')} ЦДЗ: {cdz} · {em('done')} сделано: {done}"
    body = "\n\n".join(hw_block(h) for h in items) or "Домашних заданий нет."
    kb = nav("hw", d, today=today) + [[{"text": "Решить ДЗ", "callback_data": "solve"}]]
    return f"{head}\n\n{body}", {"inline_keyboard": kb}


async def view_week(monday):
    today = datetime.now(mes.TZ).date()
    items = await hw_for(monday, monday + timedelta(days=6))
    head = (f"{em('week')} <b>Неделя {monday.strftime('%d.%m')}–{(monday + timedelta(days=6)).strftime('%d.%m')}</b>\n"
            f"Всего: {len(items)} · {em('cdz')} ЦДЗ: {sum(1 for h in items if h.get('materials'))} · "
            f"{em('done')} сделано: {sum(1 for h in items if h.get('is_done'))}")
    parts, cur = [], None
    for h in items:
        if h.get("date") != cur:
            cur = h.get("date")
            try:
                parts.append(f"\n{em('today')} <u>{day_title(datetime.fromisoformat(cur).date(), today)}</u>")
            except (TypeError, ValueError):
                parts.append(f"\n<u>{esc(cur)}</u>")
        parts.append(hw_block(h))
    kb = [[{"text": "◀", "callback_data": f"wk:{(monday - timedelta(days=7)).isoformat()}"},
           {"text": "Эта неделя", "callback_data": f"wk:{(today - timedelta(days=today.weekday())).isoformat()}"},
           {"text": "▶", "callback_data": f"wk:{(monday + timedelta(days=7)).isoformat()}"}]]
    return head + "\n" + "\n\n".join(parts) if parts else head + "\n\nДомашних заданий нет.", {"inline_keyboard": kb}


async def view_sched(d):
    today = datetime.now(mes.TZ).date()
    p = await mes.me()
    evs = await nt._events(p, d, d)
    rows = nt.lessons(evs, d.isoformat())
    body = "\n".join(f"• {esc(r)}" for r in rows) or "Уроков нет."
    return f"{em('sched')} <b>Расписание · {day_title(d, today)}</b>\n\n{body}", {"inline_keyboard": nav("sc", d, today=today)}


async def view_cdz():
    today = datetime.now(mes.TZ).date()
    items = [h for h in await hw_for(today, today + timedelta(days=7)) if h.get("materials")]
    rows = []
    for h in items:
        mats = ", ".join(clean(m.get("title") or "материал", 50) for m in h["materials"] if isinstance(m, dict))
        rows.append(f"{em('done') if h.get('is_done') else em('cdz')} <b>{esc(h.get('subject_name'))}</b> на {esc(h.get('date'))}\n{esc(mats)}")
    return f"{em('cdz')} <b>ЦДЗ на неделю</b>\n\n" + ("\n\n".join(rows) or "Заданий с материалами МЭШ нет."), None


async def view_marks():
    p = await mes.me()
    today = datetime.now(mes.TZ).date()
    recent = await nt._marks(p, today - timedelta(days=7), today)
    data = await mes.get("family/web/v1/subject_marks", {"student_id": p["student_id"]}, profile_id=p["profile_id"], ttl=0)
    items = (data.get("payload") if isinstance(data, dict) else data) or []
    txt = f"{em('marks')} <b>Оценки</b>\n\n"
    if recent:
        txt += "<b>За неделю</b>\n" + "\n".join(f"• {esc(m.get('date'))} {esc(nt.mark_line(m))}" for m in recent) + "\n\n"
    txt += "<b>По предметам</b>\n<pre>" + esc(mes.fmt_subject_marks(items) or "данных нет") + "</pre>"
    return txt, None


async def view_food(d):
    today = datetime.now(mes.TZ).date()
    p = await mes.me()
    out = []
    for c, free in await mes.menu_complexes(p, d, "всё"):
        rows = []
        for ci in c.get("complexItems") or []:
            x = ci.get("dish") or ci
            w = f" · {x['weight']} г" if x.get("weight") else ""
            rows.append(f"• {esc(x.get('name', '?'))}{w}")
        tag = "🆓 льготное" if free else "платное"
        out.append(f"<b>{esc(c.get('name'))}</b> · {tag} · {esc(mes.price_label(c, free))}\n" + "\n".join(rows))
    bal = ""
    try:
        b = await mes.food("food/meals/v3/clients/balance", p)
        if isinstance(b, list) and b:
            bal = f"\n{em('money')} На карте: {esc(mes.rub(b[0].get('balance')))}"
    except mes.MesError:
        pass
    txt = f"{em('food')} <b>Меню · {day_title(d, today)}</b>{bal}\n\n" + ("\n\n".join(out) or "Меню на этот день нет.")
    return txt, {"inline_keyboard": nav("fd", d, today=today)}


def view_settings():
    on = lambda k, default=True: ("включена" if not nt.st.get(k) else "выключена") if default else ("включена" if nt.st.get(k) else "выключена")  # noqa: E731
    txt = (f"{em('settings')} <b>Настройки</b>\n"
           f"{em('bell')} Уведомления об оценках, ДЗ и заменах: <b>{on('notify_off')}</b>\n"
           f"{em('today')} Утренняя сводка: <b>{on('digest_off')}</b>\n"
           f"{em('tomorrow')} Рассылка ДЗ на завтра (18:00): <b>{on('hw_evening', False)}</b>")
    kb = [[{"text": "Уведомления", "callback_data": "set:notify_off"}], [{"text": "Утренняя сводка", "callback_data": "set:digest_off"}],
          [{"text": "ДЗ на завтра вечером", "callback_data": "set:hw_evening"}]]
    return txt, {"inline_keyboard": kb}


async def view_solve_list():
    today = datetime.now(mes.TZ).date()
    items = [h for h in await hw_for(today, today + timedelta(days=7)) if not h.get("is_done")]
    if not items:
        return f"{em('party')} Несделанных заданий нет.", None
    kb = [[{"text": f"{h.get('subject_name')} · {(h.get('date') or '')[5:]}"[:60],
            "callback_data": f"sv:{h.get('homework_entry_id') or h.get('homework_id')}"}] for h in items[:12]]
    return f"{em('solve')} <b>Что решаем?</b>\nВыберите предмет: прочитаю задание (и фото) и решу.", {"inline_keyboard": kb}


async def gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise mes.MesError("на сервере нет GEMINI_API_KEY")
    last = ""
    async with httpx.AsyncClient(proxy=nt._proxy(), timeout=120) as h:
        for model in (os.environ.get("MES_VISION_MODEL", "gemini-3.6-flash"), "gemini-3.5-flash-lite"):
            r = await h.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                             json={"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}},
                             headers={"x-goog-api-key": key, "content-type": "application/json"})
            if r.status_code == 200:
                try:
                    t = "".join(x.get("text", "") for x in r.json()["candidates"][0]["content"]["parts"]).strip()
                    if t:
                        return t
                except (KeyError, IndexError, ValueError):
                    pass
            last = str(r.status_code)
    raise mes.MesError(f"модель не ответила ({last})")


async def solve(hid):
    today = datetime.now(mes.TZ).date()
    h = next((x for x in await hw_for(today - timedelta(days=3), today + timedelta(days=14))
              if str(x.get("homework_entry_id") or x.get("homework_id")) == hid), None)
    if not h:
        return "Не нашёл это задание: возможно, оно уже убрано из дневника."
    src = [f"Предмет: {h.get('subject_name')}", f"Текст задания: {h.get('description') or '(нет)'}"]
    for title, url in mes.file_links(h)[:4]:
        try:
            raw, ctype = await mes.download(url)
            if ctype in mes.IMAGE_TYPES:
                src.append(f"Фото «{title}»:\n{await mes.read_photo(raw, ctype)}")
        except mes.MesError as e:
            src.append(f"[файл {title}: {e}]")
    prompt = ("Ты помогаешь школьнику (7–9 класс) с домашним заданием. Реши все задания ниже. Пиши по-русски, коротко, с ходом решения "
              "и ответом к каждой задаче. Формулы обычным текстом, без LaTeX и без markdown-разметки. Если данных не хватает, скажи, чего именно.\n\n"
              + "\n\n".join(src))
    return "\n".join(src[:2]) + "\n\n" + await gemini(prompt)


# ---------- обработка ----------

SIMPLE = {"Сегодня", "Завтра", "Неделя", "Расписание", "ЦДЗ", "Оценки", "Решить ДЗ", "Еда", "Настройки"}


async def show(name, edit=None, day=None):
    today = datetime.now(mes.TZ).date()
    if name == "Сегодня":
        txt, mk = await view_hw(day or today)
    elif name == "Завтра":
        txt, mk = await view_hw(day or today + timedelta(days=1))
    elif name == "Неделя":
        txt, mk = await view_week(day or today - timedelta(days=today.weekday()))
    elif name == "Расписание":
        txt, mk = await view_sched(day or today)
    elif name == "ЦДЗ":
        txt, mk = await view_cdz()
    elif name == "Оценки":
        txt, mk = await view_marks()
    elif name == "Решить ДЗ":
        txt, mk = await view_solve_list()
    elif name == "Еда":
        txt, mk = await view_food(day or today)
    else:
        txt, mk = view_settings()
    await say(txt, mk, edit=edit)


async def on_message(m):
    text = (m.get("text") or "").strip()
    if text in ("/start", "/menu"):
        await say(f"{em('info')} <b>Дневник МЭШ</b>\nВыберите раздел кнопками внизу.", keyboard())
    elif text in SIMPLE:
        await show(text)


async def on_callback(c):
    await api("answerCallbackQuery", callback_query_id=c["id"])
    data, mid = c.get("data") or "", (c.get("message") or {}).get("message_id")
    kind, _, arg = data.partition(":")
    try:
        if kind in ("hw", "wk", "sc", "fd"):
            d = datetime.fromisoformat(arg).date()
            await show({"hw": "Сегодня", "wk": "Неделя", "sc": "Расписание", "fd": "Еда"}[kind], edit=mid, day=d)
        elif kind == "solve":
            await show("Решить ДЗ")
        elif kind == "sv":
            await say(f"{em('solve')} Читаю задание и решаю, это займёт до минуты…")
            await say(esc(await solve(arg)))
        elif kind == "set" and arg in ("notify_off", "digest_off", "hw_evening"):
            nt.st[arg] = not nt.st.get(arg)
            nt.save()
            txt, mk = view_settings()
            await say(txt, mk, edit=mid)
    except mes.MesAuthError:
        await say(f"{em('warn')} Вход в МЭШ слетел. Пришлите новые токены.")
    except (mes.MesError, ValueError) as e:
        await say(f"{em('warn')} Не получилось: {esc(clean(str(e), 200))}")


async def loop():
    await asyncio.sleep(5)
    await api("deleteWebhook")
    code, _ = await api("setMyCommands", commands=[{"command": "menu", "description": "Показать кнопки"}])
    if not nt.st.get("kb_sent") and code == 200:
        await say(f"{em('info')} <b>Дневник МЭШ</b>\nВыберите раздел кнопками внизу.", keyboard())
        nt.st["kb_sent"] = True
        nt.save()
    offset = 0
    while True:
        try:
            code, r = await api("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query"], _timeout=40)
            if code != 200:
                await asyncio.sleep(10 if code else 20)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message")
                cb = u.get("callback_query")
                who = ((msg or {}).get("chat") or {}).get("id") or ((cb or {}).get("message") or {}).get("chat", {}).get("id")
                if str(who) != nt.chat():
                    continue
                try:
                    if msg:
                        await on_message(msg)
                    elif cb:
                        await on_callback(cb)
                except mes.MesAuthError:
                    await say(f"{em('warn')} Вход в МЭШ слетел. Пришлите новые токены.")
                except mes.MesError as e:
                    await say(f"{em('warn')} {esc(clean(str(e), 200))}")
        except Exception as e:  # noqa: BLE001
            log(f"ошибка цикла ({type(e).__name__}: {clean(str(e), 100)})")
            await asyncio.sleep(10)


def start():
    global _task
    if os.environ.get("MES_BOT_UI", "1") == "0" or not nt.enabled():
        return
    _task = asyncio.get_running_loop().create_task(loop())
    log("кнопочное меню включено")
