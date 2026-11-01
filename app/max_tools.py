"""Инструменты MAX (web.max.ru) для того же MCP-сервера. Вход: sudo bash deploy/max_login.sh."""
import asyncio
import collections
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from pymax import File, Photo, Video
from pymax.types.domain.attachments.enums import PollFlags
from pymax.types.domain.attachments.poll import Poll, PollAnswer

import media
from dates import parse_when
from maxcfg import make_client, session_exists
from safety import clean, name

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
HISTORY_MAX = 5000      # сообщений за один вызов
HISTORY_CHARS = 80000   # символов в ответе: больше ChatGPT всё равно не прочитает

_client = None
_lock = asyncio.Lock()


async def mx():
    """Постоянное подключение к MAX; переподключается, если связь оборвалась."""
    global _client
    if not session_exists():
        raise RuntimeError("Сессия MAX не создана: выполните max_login.sh")
    async with _lock:
        if _client is None or not _client.is_connected or _client.me is None:
            if _client is not None:
                try:
                    await _client.close()
                except Exception:
                    pass
            _client = make_client()
            await _client.connect()
            if _client.me is None:
                raise RuntimeError("Не удалось войти в MAX: выполните max_login.sh заново")
        return _client


def uname(u):
    if u is None:
        return "?"
    for n in u.names:
        s = n.name or " ".join(x for x in (n.first_name, n.last_name) if x)
        if s:
            return name(s)
    return str(u.id)


async def names(c, ids):
    ids = [i for i in dict.fromkeys(ids) if i]
    try:
        return {u.id: uname(u) for u in await c.get_users(ids)} if ids else {}
    except Exception:
        return {}


def peer(c, chat):
    return next((p for p in chat.participants if p != c.me.contact.id), None)


def atype(a):
    return str(getattr(a, "type", "")).split(".")[-1]


def kind(m):
    out = []
    for a in m.attaches:
        t = atype(a)
        if t == "POLL":
            out.append(f"<опрос: {clean(a.title, 300)}>")
        elif t == "FILE":
            out.append(f"<файл: {name(a.name)}>")
        elif t:
            out.append(f"<{ {'PHOTO': 'фото', 'VIDEO': 'видео', 'STICKER': 'стикер', 'AUDIO': 'аудио'}.get(t, t.lower()) }>")
    return " ".join(out)


def fmt(m, who, text_limit=4000):
    t = datetime.fromtimestamp(m.time / 1000, TZ)
    body = " ".join(x for x in (kind(m), clean(m.text, text_limit)) if x)
    return f"#{m.id} [{t:%Y-%m-%d %H:%M}] {who}: {body}"


async def pages(c, chat_id, lo=None, hi=None, before_id=0):
    """Страницы истории от новых к старым (по 100 сообщений, внутри страницы тоже от новых к старым).

    lo / hi — границы по времени (datetime), before_id — идти от сообщения с этим id назад."""
    cid = int(chat_id)
    lo_ms, hi_ms = (int(lo.timestamp() * 1000) if lo else None), (int(hi.timestamp() * 1000) if hi else None)
    t = hi_ms
    if before_id:
        m = await c.get_message(cid, before_id)
        if m:
            t = m.time if t is None else min(t, m.time)
    seen = {before_id}
    while True:
        page = await c.fetch_history(cid, backward=100, **({"from_time": t} if t else {}))
        fresh = sorted((m for m in page if m.id not in seen), key=lambda m: m.time, reverse=True)
        if not fresh:
            return
        seen.update(m.id for m in fresh)
        t = fresh[-1].time
        yield [m for m in fresh if (hi_ms is None or m.time <= hi_ms) and (lo_ms is None or m.time >= lo_ms)]
        if (lo_ms and t < lo_ms) or len(page) < 2:
            return


async def history(c, chat_id, limit, date_from="", date_to="", before_id=0, text_limit=1000):
    """Сообщения с отсечкой по датам и размеру: (строки от старых к новым, подсказка)."""
    lo, hi = parse_when(date_from, TZ), parse_when(date_to, TZ, end=True)
    if lo and hi and lo > hi:
        raise ValueError("date_from позже date_to")
    limit = max(1, min(limit, HISTORY_MAX))
    got, size, stopped = [], 0, False
    async for page in pages(c, chat_id, lo, hi, before_id):
        for m in page:
            if got and (len(got) >= limit or size + min(len(m.text or ""), text_limit) + 40 > HISTORY_CHARS):
                stopped = True
                break
            got.append(m)
            size += min(len(m.text or ""), text_limit) + 40
        if stopped:
            break
    me = c.me.contact.id
    who = await names(c, [m.sender for m in got if m.sender != me])
    lines = [fmt(m, "я" if m.sender == me else who.get(m.sender, str(m.sender)), text_limit) for m in reversed(got)]
    hint = ""
    if stopped:
        hint = f"… показано {len(got)}, есть более старые. Продолжить: before_id={got[-1].id}" + (f", date_from={date_from}" if date_from else "")
    return lines, hint


def to_attach(data, fname, mime):
    if mime.startswith("image/") and not mime.endswith("gif"):
        return Photo(raw=data, name=fname)
    if mime.startswith("video/"):
        return Video(raw=data, name=fname)
    return File(raw=data, name=fname)


async def profile(c, **change):
    """change_profile требует имя целиком: подставляем текущие значения для незаданных полей."""
    n = c.me.contact.names[0] if c.me.contact.names else None
    cur = dict(first_name=(n.first_name if n else "") or uname(c.me.contact),
               last_name=n.last_name if n else None, description=c.me.contact.description)
    cur.update({k: v for k, v in change.items() if v})
    await c.change_profile(**cur)


def register(tool, send_guard, mark_sent, Image):
    # ---------- чтение ----------

    @tool()
    async def max_list_chats(limit: int = 30) -> str:
        """MAX: последние чаты — id, название, тип, непрочитанные."""
        c = await mx()
        chats = sorted(await c.fetch_chats(), key=lambda x: x.last_event_time, reverse=True)[:limit]
        who = await names(c, [peer(c, ch) for ch in chats if not ch.title])
        out = []
        for ch in chats:
            t = {"DIALOG": "личный", "CHAT": "группа", "CHANNEL": "канал"}.get(str(ch.type).split(".")[-1], str(ch.type))
            st = "" if ch.status in ("ACTIVE", "", None) else f" | {ch.status}"
            title = name(ch.title) if ch.title else who.get(peer(c, ch), "диалог")
            out.append(f"{ch.id} | {title} | {t} | unread={ch.new_messages}{st}")
        return "\n".join(out) or "Пусто."

    @tool()
    async def max_list_folders() -> str:
        """MAX: папки чатов и id чатов в них (архива как отдельной папки в MAX нет)."""
        c = await mx()
        fl = await c.get_folders()
        return "\n".join(f"{f.id} | {name(f.title)} | чаты: {', '.join(map(str, f.include)) or '-'}"
                         for f in fl.folders) or "Папок нет."

    @tool()
    async def max_read_messages(chat: str, limit: int = 30, before_id: int = 0, date_from: str = "", date_to: str = "",
                                text_limit: int = 1000) -> str:
        """MAX: история чата (id из max_list_chats), старые сверху. #N — id сообщения.

        Любая глубина: limit до 5000. date_from / date_to — период (2025-03, 2025-11-01 14:30, 31.10.2025, 1y, «год назад»):
        «сообщения за март прошлого года» = date_from=2025-03, date_to=2025-03, limit=5000.
        Если ответ обрезан, в конце подсказка before_id для продолжения. text_limit — обрезать длинные сообщения."""
        c = await mx()
        lines, hint = await history(c, chat, limit, date_from, date_to, before_id, text_limit)
        if not lines:
            return "Сообщений за этот период нет."
        head = f"{len(lines)} сообщений, с {lines[0][lines[0].index('[') + 1:][:16]} по {lines[-1][lines[-1].index('[') + 1:][:16]}"
        return "\n".join([head, *lines, *([hint] if hint else [])])

    @tool()
    async def max_messages_around_date(chat: str, when: str, around: int = 10) -> str:
        """MAX: сообщения вокруг даты — around до и around после (2025-03-14, «год назад», 31.10.2025 18:00)."""
        c = await mx()
        t = parse_when(when, TZ)
        if t is None:
            raise ValueError("Укажите дату")
        around = max(1, min(around, 200))
        msgs = await c.fetch_history(int(chat), from_time=int(t.timestamp() * 1000), backward=around, forward=around)
        msgs = sorted(msgs, key=lambda m: m.time)
        me = c.me.contact.id
        who = await names(c, [m.sender for m in msgs if m.sender != me])
        return "\n".join(fmt(m, "я" if m.sender == me else who.get(m.sender, str(m.sender)), 600) for m in msgs) or "Рядом с этой датой сообщений нет."

    @tool()
    async def max_chat_timeline(chat: str, date_from: str = "", date_to: str = "", step: str = "month", scan: int = 20000) -> str:
        """MAX: активность чата по периодам (step: year, month, week) и самые активные участники.

        MAX не отдаёт счётчики, поэтому сервер просматривает до scan сообщений (по умолчанию 20000, около 10–20 с).
        Находит, когда шла переписка; потом читайте нужный период через max_read_messages(date_from, date_to)."""
        c = await mx()
        lo, hi = parse_when(date_from, TZ), parse_when(date_to, TZ, end=True)
        key = {"year": "%Y", "week": "%G-W%V"}.get(step, "%Y-%m")
        per, who, total, last = collections.Counter(), collections.Counter(), 0, None
        async for page in pages(c, chat, lo, hi):
            for m in page:
                per[datetime.fromtimestamp(m.time / 1000, TZ).strftime(key)] += 1
                who[m.sender] += 1
                last = m
            total += len(page)
            if total >= scan:
                break
        if not per:
            return "Сообщений за этот период нет."
        top = max(per.values())
        rows = [f"{k}  {per[k]:>6}  {'█' * max(1, round(24 * per[k] / top))}" for k in sorted(per)]
        known = await names(c, [u for u, _ in who.most_common(10)])
        people = ", ".join(f"{known.get(u, u)} {n}" for u, n in who.most_common(8))
        oldest = f"{datetime.fromtimestamp(last.time / 1000, TZ):%Y-%m-%d}"
        more = " (дальше есть ещё: увеличьте scan)" if total >= scan else ""
        return f"Просмотрено {total}, самое раннее: {oldest}{more}\n" + "\n".join(rows) + f"\nАктивнее всех: {people}"

    @tool()
    async def max_search_messages(chat: str, query: str, limit: int = 20, depth: int = 300, date_from: str = "",
                                  date_to: str = "", before_id: int = 0) -> str:
        """MAX: поиск по тексту и именам файлов в чате (MAX не ищет на сервере, поэтому просматриваются сообщения подряд).

        depth — сколько последних сообщений просматривать (до 20000: «найди, что мне писали год назад»);
        date_from / date_to сужают период и ускоряют поиск. limit до 5000; дальше — подсказка before_id."""
        q = query.lower()
        c = await mx()
        lo, hi = parse_when(date_from, TZ), parse_when(date_to, TZ, end=True)
        scanned = 0
        limit = max(1, min(limit, HISTORY_MAX))
        depth = max(1, min(depth, 20000))
        found, stopped = [], False
        async for page in pages(c, chat, lo, hi, before_id):
            for m in page:
                scanned += 1
                files = " ".join(getattr(a, "name", "") or "" for a in m.attaches)
                if q in f"{m.text or ''} {files}".lower():
                    found.append(m)
                    if len(found) >= limit:
                        stopped = True
                        break
            if stopped or scanned >= depth:
                break
        me = c.me.contact.id
        who = await names(c, [m.sender for m in found if m.sender != me])
        lines = [fmt(m, "я" if m.sender == me else who.get(m.sender, str(m.sender)), 600) for m in reversed(found)]
        if stopped:
            tail = f"… просмотрено {scanned}, найдено {len(found)}, дальше: before_id={found[-1].id}"
        else:
            tail = f"… просмотрено {scanned} сообщений" + (", есть более старые: увеличьте depth" if scanned >= depth else "")
        return "\n".join([*lines, tail]) if found else f"Ничего не найдено ({tail[2:]})."

    @tool()
    async def max_get_media(chat: str, message_id: int):
        """MAX: показать фото (или превью видео) из сообщения как картинку."""
        c = await mx()
        m = await c.get_message(int(chat), message_id)
        url = next((u for a in (m.attaches if m else [])
                    if (u := getattr(a, "base_url", None) or getattr(a, "thumbnail", None))), None)
        if not url:
            return "В сообщении нет фото или видео."
        data, _, _ = await media.get(url, 20 * 1024 * 1024)  # ссылка из чужого сообщения: только публичные адреса
        return [f"{kind(m)} из сообщения #{m.id}", Image(data=data, format="jpeg")]

    @tool()
    async def max_chat_members(chat: str, limit: int = 50) -> str:
        """MAX: участники группы или канала."""
        c = await mx()
        members, _ = await c.get_chat_members(int(chat), count=limit)
        return "\n".join(f"{m.contact.id} | {uname(m.contact)}" for m in members) or "Участников не видно."

    @tool()
    async def max_get_profile() -> str:
        """MAX: мой профиль."""
        c = await mx()
        u = c.me.contact
        return f"id={u.id}\nимя={uname(u)}\nтелефон={u.phone or ''}\nописание={clean(u.description)}"

    # ---------- отправка ----------

    @tool(write=True)
    async def max_send_message(chat: str, text: str, reply_to: int = 0) -> str:
        """MAX: отправить сообщение от имени владельца аккаунта. reply_to — id сообщения для ответа."""
        err = send_guard("max", chat, text)
        if err:
            return err
        c = await mx()
        m = await c.send_message(int(chat), text, reply_to=reply_to or None)
        mark_sent("max", chat, m.id, f"len={len(text)}")
        return f"Отправлено в MAX (id {m.id})."

    @tool(write=True, meta=media.FILE_META)
    async def max_send_file(chat: str, file: str | dict, caption: str = "") -> str:
        """MAX: отправить картинку/видео/файл — ссылка http(s) или файл, прикреплённый в чате ChatGPT."""
        err = send_guard("max", chat)
        if err:
            return err
        data, fname, mime = await media.fetch(file)
        c = await mx()
        m = await c.send_message(int(chat), caption or None, attachments=[to_attach(data, fname, mime)])
        mark_sent("max", chat, m.id, f"file={fname}")
        return f"Отправлено в MAX (id {m.id})."

    @tool(write=True)
    async def max_send_poll(chat: str, question: str, options: list[str], multiple: bool = False,
                            anonymous: bool = True, quiz: bool = False) -> str:
        """MAX: создать опрос (2–12 вариантов)."""
        err = send_guard("max", chat, question)
        if err:
            return err
        if not 2 <= len(options) <= 12:
            return "Отказ: нужно от 2 до 12 вариантов."
        flags = PollFlags(0)
        for on, f in ((multiple, PollFlags.FLAG_SETTINGS_MULTISELECT), (anonymous, PollFlags.FLAG_SETTINGS_ANONYMOUS),
                      (quiz, PollFlags.FLAG_SETTINGS_QUIZ)):
            if on:
                flags |= f
        poll = Poll(title=question, answers=[PollAnswer(text=o) for o in options], settings=flags)
        c = await mx()
        m = await c.send_message(int(chat), None, attachments=[poll])
        mark_sent("max", chat, m.id, "poll")
        return f"Опрос отправлен в MAX (id {m.id})."

    @tool(write=True)
    async def max_vote_poll(chat: str, message_id: int, options: list[int]) -> str:
        """MAX: проголосовать в опросе — номера вариантов с 0."""
        c = await mx()
        m = await c.get_message(int(chat), message_id)
        poll = next((a for a in (m.attaches if m else []) if atype(a) == "POLL"), None)
        if poll is None:
            return "Это не опрос."
        await c.vote_poll(int(chat), message_id, poll.poll_id, [poll.answers[i].answer_id for i in options])
        return "Голос учтён."

    @tool(write=True)
    async def max_forward_message(from_chat: str, message_id: int, to_chat: str) -> str:
        """MAX: переслать сообщение в другой чат."""
        err = send_guard("max", to_chat)
        if err:
            return err
        c = await mx()
        m = await c.forward_message(int(to_chat), message_id, int(from_chat))
        mark_sent("max", to_chat, m.id, "forward")
        return f"Переслано (id {m.id})."

    # ---------- управление ----------

    @tool(write=True)
    async def max_edit_message(chat: str, message_id: int, text: str) -> str:
        """MAX: изменить текст своего сообщения."""
        c = await mx()
        await c.edit_message(int(chat), message_id, text)
        return "Изменено."

    @tool(write=True, destructive=True)
    async def max_delete_messages(chat: str, message_ids: list[int], for_everyone: bool = True) -> str:
        """MAX: удалить сообщения (for_everyone=false — только у себя)."""
        c = await mx()
        await c.delete_message(int(chat), message_ids, for_me=not for_everyone)
        print(f"MAX DELETE chat={chat} ids={message_ids}", file=sys.stderr, flush=True)
        return "Удалено."

    @tool(write=True)
    async def max_pin_message(chat: str, message_id: int) -> str:
        """MAX: закрепить сообщение."""
        c = await mx()
        await c.pin_message(int(chat), message_id, notify_pin=False)
        return "Закреплено."

    @tool(write=True)
    async def max_react(chat: str, message_id: int, emoji: str = "👍") -> str:
        """MAX: поставить реакцию (пустая строка — убрать свою)."""
        c = await mx()
        if emoji:
            await c.add_reaction(int(chat), message_id, emoji)
        else:
            await c.remove_reaction(int(chat), message_id)
        return "Готово."

    @tool(write=True)
    async def max_mark_read(chat: str) -> str:
        """MAX: отметить чат прочитанным."""
        c = await mx()
        last = await c.fetch_history(int(chat), backward=1)
        if last:
            await c.read_message(last[-1].id, int(chat))
        return "Прочитано."

    @tool(write=True)
    async def max_create_group(title: str, user_ids: list[int]) -> str:
        """MAX: создать группу с пользователями (id пользователей)."""
        c = await mx()
        r = await c.create_group(title, user_ids)
        return f"Группа создана: id={r[0].id}" if r else "MAX не вернул данные группы."

    @tool(write=True)
    async def max_join_by_link(link: str) -> str:
        """MAX: вступить в группу или канал по ссылке-приглашению."""
        c = await mx()
        ch = await (c.join_channel(link) if "/channel" in link else c.join_group(link))
        return f"Вступил: {ch.id} | {name(ch.title)}"

    @tool(write=True, destructive=True)
    async def max_leave_chat(chat: str) -> str:
        """MAX: выйти из группы или канала."""
        c = await mx()
        await c.leave_group(int(chat))
        return "Вышел."

    # ---------- профиль ----------

    @tool(write=True)
    async def max_update_profile(first_name: str = "", last_name: str = "", description: str = "") -> str:
        """MAX: изменить имя, фамилию или описание (пустые поля не меняются)."""
        await profile(await mx(), first_name=first_name, last_name=last_name, description=description)
        return "Профиль MAX обновлён."

    @tool(write=True, meta=media.FILE_META)
    async def max_set_avatar(file: str | dict) -> str:
        """MAX: поставить новую аватарку — ссылка на картинку или файл из чата ChatGPT."""
        data, fname, _ = await media.fetch(file)
        await profile(await mx(), photo=Photo(raw=data, name=fname))
        return "Аватарка MAX обновлена."
