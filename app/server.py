"""MCP-сервер для личных аккаунтов Telegram и MAX (HTTP для ChatGPT/Claude, stdio для локальных клиентов).

Защита: секретный путь /<TG_SECRET>/mcp, белые списки отправки TG_SEND_ALLOW / MAX_SEND_ALLOW
("me" = Избранное, id чатов через запятую, "*" = любые), лимит TG_SEND_PER_MIN,
READ_ONLY=1 — только чтение.
"""
import asyncio
import collections
import functools
import io
import os
import random
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from telethon import events, functions, types, utils

import dialogs
import media
from dates import parse_when
from safety import INSTRUCTIONS, clean, name
from tgcfg import make_client

STDIO = "--stdio" in sys.argv
SECRET = os.environ.get("TG_SECRET", "")
HOST = os.environ.get("TG_HOST", "127.0.0.1")  # в Docker — 0.0.0.0
PORT = int(os.environ.get("TG_PORT", "8765"))
SEND_ALLOW = {x.strip().lower() for x in os.environ.get("TG_SEND_ALLOW", "").split(",") if x.strip()}
MAX_SEND_ALLOW = {x.strip() for x in os.environ.get("MAX_SEND_ALLOW", "").split(",") if x.strip()}
SEND_PER_MIN = int(os.environ.get("TG_SEND_PER_MIN", "10"))
READ_ONLY = os.environ.get("READ_ONLY", "").lower() in ("1", "true", "yes")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))

if not STDIO and len(SECRET) < 24:
    raise SystemExit("TG_SECRET должен быть не короче 24 символов")

HTTP_OPTS = dict(
    streamable_http_path=f"/{SECRET}/mcp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
mcp = MCPServer("tg-max-mcp", instructions=INSTRUCTIONS)


def tool(write=False, destructive=False, **kw):
    """mcp.tool с подсказками для клиента; ошибка возвращается модели текстом.

    Инструменты чтения помечены read-only — ChatGPT вызывает их без подтверждения.
    При READ_ONLY=1 инструменты записи не регистрируются.
    """
    def deco(fn):
        if write and READ_ONLY:
            return fn

        @functools.wraps(fn)
        async def wrapper(*a, **k):
            try:
                return await fn(*a, **k)
            except Exception as e:
                traceback.print_exc()
                return f"Ошибка: {type(e).__name__}: {e}"
        ann = ToolAnnotations(read_only_hint=not write, destructive_hint=destructive, open_world_hint=True)
        return mcp.tool(annotations=ann, **kw)(wrapper)
    return deco


_sent = collections.deque()
_client = None
_client_lock = asyncio.Lock()


async def tg():
    """Постоянное подключение к Telegram: без переподключения на каждый вызов."""
    global _client
    async with _client_lock:
        if _client is None:
            _client = make_client()
            _client.add_event_handler(_tg_incoming, events.NewMessage(incoming=True))
        if not _client.is_connected():
            await _client.connect()
            if not await _client.is_user_authorized():
                await _client.disconnect()
                raise RuntimeError("Сессия Telegram не авторизована: выполните login.sh")
        return _client


def norm(chat):
    s = str(chat).strip()
    if s.lower() in ("me", "saved", "избранное"):
        return "me"
    try:
        return int(s)
    except ValueError:
        return s  # @username


async def resolve(c, key):
    try:
        return await c.get_input_entity(key)
    except ValueError:
        await c.get_dialogs(limit=300)  # прогреть кэш сущностей новой сессии
        return await c.get_input_entity(key)


async def peer(chat):
    c = await tg()
    return c, await resolve(c, norm(chat))


def kind(m):
    if m.poll:
        q = m.poll.poll.question
        return f"<опрос: {clean(getattr(q, 'text', q), 300)}>"
    for attr, label in (("photo", "фото"), ("sticker", "стикер"), ("voice", "голосовое"),
                        ("video_note", "кружок"), ("video", "видео"), ("gif", "gif"), ("audio", "аудио")):
        if getattr(m, attr, None):
            return f"<{label}>"
    if m.document:
        return f"<файл: {name(m.file.name or m.file.mime_type)}>"
    return "<медиа>" if m.media else ""


def fmt(m, text_limit=4000):
    s = m.sender
    who = "я" if m.out else (person(s) if s is not None else m.sender_id)
    reply = f" ↩{m.reply_to_msg_id}" if m.reply_to_msg_id else ""
    btn = " [кнопки]" if m.buttons else ""
    body = " ".join(x for x in (kind(m), clean(m.text, text_limit)) if x)
    return f"#{m.id} [{m.date.astimezone(TZ):%Y-%m-%d %H:%M}] {who}{reply}{btn}: {body}"


HISTORY_MAX = 5000      # сообщений за один вызов
HISTORY_CHARS = 80000   # символов в ответе: больше ChatGPT всё равно не прочитает


async def history(c, ent, limit, date_from="", date_to="", before_id=0, text_limit=1000, search=None, from_user=None,
                  prefix=lambda m: ""):
    """Сообщения от новых к старым с отсечкой по датам и размеру; возвращает (строки от старых к новым, подсказка)."""
    lo, hi = parse_when(date_from, TZ), parse_when(date_to, TZ, end=True)
    if lo and hi and lo > hi:
        raise ValueError("date_from позже date_to")
    limit = max(1, min(limit, HISTORY_MAX))
    out, size, oldest, stopped = [], 0, None, False
    async for m in c.iter_messages(ent, search=search, from_user=from_user, offset_id=before_id or 0, offset_date=hi,
                                   wait_time=0):
        if lo and m.date < lo:
            break
        line = prefix(m) + fmt(m, text_limit)
        if out and (len(out) >= limit or size + len(line) > HISTORY_CHARS):
            stopped = True
            break
        out.append(line)
        size += len(line) + 1
        oldest = m
    if not stopped:
        return list(reversed(out)), ""
    # в одном чате продолжают по id, при сквозном поиске id у чатов свои — по дате
    cont = f"before_id={oldest.id}" if ent is not None else f"date_to={oldest.date.astimezone(TZ):%Y-%m-%d %H:%M:%S}"
    return list(reversed(out)), f"… показано {len(out)}, есть более старые. Продолжить: {cont}" + (f", date_from={date_from}" if date_from else "")


def send_guard(net, chat, text=None):
    """Белый список, длина и общий для Telegram и MAX лимит в минуту. None = можно."""
    allow, key = (SEND_ALLOW, str(norm(chat)).lower()) if net == "tg" else (MAX_SEND_ALLOW, str(chat).strip())
    if "*" not in allow and key not in allow:
        return f"Отказ: чат {chat} не в белом списке отправки."
    if text is not None and (not text.strip() or len(text) > 4000):
        return "Отказ: пустой или слишком длинный текст."
    now = time.time()
    while _sent and now - _sent[0] > 60:
        _sent.popleft()
    if len(_sent) >= SEND_PER_MIN:
        return f"Отказ: лимит {SEND_PER_MIN} сообщений в минуту."
    return None


def mark_sent(net, chat, msg_id, what):
    _sent.append(time.time())
    print(f"{net.upper()} SEND chat={chat} id={msg_id} {what}", file=sys.stderr, flush=True)


def as_file(data, fname):
    bio = io.BytesIO(data)
    bio.name = fname
    return bio


def person(u):
    full = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x)
    return name(full or getattr(u, "title", "") or str(u.id))


# ---------- чтение ----------

@tool()
async def list_chats(limit: int = 30, archived: bool = False) -> str:
    """Чаты Telegram: id, название, тип, непрочитанные. archived=true — чаты из архива."""
    c = await tg()
    out = []
    async for d in c.iter_dialogs(folder=1 if archived else 0):
        if d.archived != archived:  # в ответ на folder=1 Telegram подмешивает закреплённые из основного списка
            continue
        if len(out) >= limit:
            break
        t = "канал" if d.is_channel and not d.is_group else "группа" if d.is_group else "личный"
        flags = [f for f, on in (("закреплён", d.pinned), ("архив", d.archived)) if on]
        out.append(f"{d.id} | {name(d.name)} | {t} | unread={d.unread_count}" + (f" | {', '.join(flags)}" if flags else ""))
    return "\n".join(out) or "Пусто."


@tool()
async def list_folders() -> str:
    """Папки Telegram (фильтры чатов) и сколько чатов в них явно включено."""
    c = await tg()
    r = await c(functions.messages.GetDialogFiltersRequest())
    out = []
    for f in getattr(r, "filters", r):
        title = getattr(getattr(f, "title", None), "text", getattr(f, "title", None))
        if title:
            out.append(f"{f.id} | {name(title)} | чатов: {len(getattr(f, 'include_peers', []))}")
    return "\n".join(out) or "Папок нет."


@tool()
async def read_messages(chat: str, limit: int = 30, before_id: int = 0, date_from: str = "", date_to: str = "",
                        text_limit: int = 1000) -> str:
    """История чата (id или @username из list_chats; "me" = Избранное), старые сверху. #N — id сообщения.

    Любая глубина: limit до 5000. date_from / date_to — период (2025-03, 2025-11-01 14:30, 31.10.2025, 1y, «год назад»):
    «сообщения за март прошлого года» = date_from=2025-03, date_to=2025-03, limit=5000.
    Без date_from просто limit последних до date_to. Если ответ обрезан, в конце подсказка before_id для продолжения.
    text_limit — обрезать длинные сообщения."""
    c, ent = await peer(chat)
    lines, hint = await history(c, ent, limit, date_from, date_to, before_id, text_limit)
    head = f"{len(lines)} сообщений" + (f", с {lines[0][lines[0].index('[') + 1:][:16]} по {lines[-1][lines[-1].index('[') + 1:][:16]}" if lines else "")
    return "\n".join([head, *lines, *([hint] if hint else [])]) if lines else "Сообщений за этот период нет."


@tool()
async def messages_around_date(chat: str, when: str, around: int = 10) -> str:
    """Сообщения вокруг даты: around до и around после (2025-03-14, «год назад», 31.10.2025 18:00). Чтобы «прыгнуть» в прошлое."""
    c, ent = await peer(chat)
    t = parse_when(when, TZ)
    if t is None:
        raise ValueError("Укажите дату")
    around = max(1, min(around, 200))
    before = [m async for m in c.iter_messages(ent, offset_date=t, limit=around)]
    after = [m async for m in c.iter_messages(ent, offset_date=t, limit=around, reverse=True)]
    return "\n".join(fmt(m, 600) for m in [*reversed(before), *after]) or "Рядом с этой датой сообщений нет."


@tool()
async def chat_timeline(chat: str, date_from: str = "", date_to: str = "", step: str = "month") -> str:
    """Активность чата по периодам: сколько сообщений было в каждом месяце/неделе/году (step: year, month, week).

    Быстро находит, когда шла переписка; потом читайте нужный месяц через read_messages(date_from, date_to)."""
    c, ent = await peer(chat)
    first = (await c.get_messages(ent, limit=1, reverse=True))
    if not first:
        return "В чате нет сообщений."
    floor = first[0].date.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    lo = max(parse_when(date_from, TZ) or floor, floor)
    hi = parse_when(date_to, TZ, end=True) or datetime.now(TZ)
    edges, t = [], lo.replace(day=1, hour=0, minute=0, second=0, microsecond=0) if step == "month" else lo
    if step == "year":
        t = lo.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif step == "week":
        t = (lo - timedelta(days=lo.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    while t <= hi and len(edges) < (60 if step == "week" else 150):
        nxt = (t.replace(year=t.year + 1) if step == "year" else t + timedelta(days=7) if step == "week"
               else t.replace(year=t.year + (t.month == 12), month=t.month % 12 + 1))
        edges.append((t, nxt - timedelta(seconds=1)))
        t = nxt

    async def newer(x):
        """Сколько сообщений новее момента x: позицию в истории отдаёт сам Telegram (поиск с датами счёт не режет)."""
        r = await c(functions.messages.GetHistoryRequest(peer=ent, offset_id=0, offset_date=x, add_offset=0, limit=1,
                                                         max_id=0, min_id=0, hash=0))
        if not r.messages:  # x старше всех сообщений
            return getattr(r, "count", 0)
        return getattr(r, "offset_id_offset", None) or 0

    bounds = [lo] + [b + timedelta(seconds=1) for _, b in edges]
    bounds[-1] = min(bounds[-1], hi)
    pos = []
    for i in range(0, len(bounds), 3):  # по 3 запроса, чтобы не упереться в лимиты Telegram
        pos += await asyncio.gather(*(newer(x) for x in bounds[i:i + 3]))
    counts = [max(0, pos[i] - pos[i + 1]) for i in range(len(edges))]
    top = max(counts) or 1
    label = {"year": "%Y", "month": "%Y-%m", "week": "%Y-%m-%d"}.get(step, "%Y-%m")
    rows = [f"{a:{label}}  {n:>6}  {'█' * max(1 if n else 0, round(24 * n / top))}" for (a, _), n in zip(edges, counts)]
    return f"Первое сообщение: {first[0].date.astimezone(TZ):%Y-%m-%d}. Всего за период: {sum(counts)}\n" + "\n".join(rows)


@tool()
async def get_message_context(chat: str, message_id: int, around: int = 5) -> str:
    """Сообщение и around сообщений до и после него."""
    c, ent = await peer(chat)
    before = [m async for m in c.iter_messages(ent, offset_id=message_id + 1, limit=around + 1)]
    after = [m async for m in c.iter_messages(ent, min_id=message_id, limit=around, reverse=True)]
    return "\n".join(fmt(m) for m in list(reversed(before)) + after)


@tool()
async def search_messages(query: str, chat: str = "", limit: int = 20, date_from: str = "", date_to: str = "",
                          from_user: str = "", before_id: int = 0) -> str:
    """Поиск по тексту: в одном чате или (chat пустой) по всем чатам; за любой период и от конкретного автора.

    date_from / date_to — период (2025-03, 31.10.2025, «год назад»), from_user — id или @username автора.
    limit до 5000; длинный ответ обрезается с подсказкой before_id."""
    c = await tg()
    ent = await resolve(c, norm(chat)) if chat else None
    sender = await resolve(c, norm(from_user)) if from_user else None
    lines, hint = await history(c, ent, limit, date_from, date_to, before_id, 600, search=query, from_user=sender,
                                prefix=(lambda m: f"[чат {m.chat_id}] ") if ent is None else (lambda m: ""))
    return "\n".join([*lines, *([hint] if hint else [])]) or "Ничего не найдено."


@tool()
async def get_pinned_messages(chat: str, limit: int = 10) -> str:
    """Закреплённые сообщения чата."""
    c, ent = await peer(chat)
    out = [fmt(m) async for m in c.iter_messages(ent, limit=limit, filter=types.InputMessagesFilterPinned)]
    return "\n".join(out) or "Закреплённых нет."


@tool()
async def get_scheduled_messages(chat: str) -> str:
    """Отложенные сообщения в чате."""
    c, ent = await peer(chat)
    r = await c(functions.messages.GetScheduledHistoryRequest(peer=ent, hash=0))
    return "\n".join(fmt(m) for m in getattr(r, "messages", [])) or "Отложенных нет."


@tool()
async def get_drafts() -> str:
    """Черновики во всех чатах."""
    c = await tg()
    out = []
    for d in await c.get_drafts():
        if d.text:
            out.append(f"{d.entity.id if d.entity else '?'} | {person(d.entity) if d.entity else '?'}: {clean(d.text, 500)}")
    return "\n".join(out) or "Черновиков нет."


@tool()
async def get_media(chat: str, message_id: int):
    """Показать фото/стикер/превью видео из сообщения как картинку."""
    c, ent = await peer(chat)
    m = await c.get_messages(ent, ids=message_id)
    if not m or not m.media:
        return "В сообщении нет медиа."
    is_img = m.photo or (m.document and (m.file.mime_type or "").startswith("image/"))
    data = await c.download_media(m, file=bytes, thumb=None if is_img else -1)
    if not data:
        return f"Не удалось скачать {kind(m)}."
    fmt_ = "png" if (m.file.mime_type or "").endswith("png") else "webp" if m.sticker else "jpeg"
    return [f"{kind(m)} из сообщения #{m.id}", Image(data=data, format=fmt_)]


@tool()
async def list_buttons(chat: str, message_id: int) -> str:
    """Инлайн-кнопки сообщения (обычно от ботов): номер и текст."""
    c, ent = await peer(chat)
    m = await c.get_messages(ent, ids=message_id)
    if not m or not m.buttons:
        return "Кнопок нет."
    flat = [b for row in m.buttons for b in row]
    return "\n".join(f"{i} | {name(b.text)}" + (f" | {b.url}" if b.url else "") for i, b in enumerate(flat))


@tool()
async def chat_info(chat: str, members: int = 0) -> str:
    """Информация о чате/пользователе; members>0 — список участников."""
    c, ent = await peer(chat)
    e = await c.get_entity(ent)
    lines = [f"id={e.id}", f"тип={type(e).__name__}"]
    for a in ("title", "first_name", "last_name", "username", "phone"):
        if getattr(e, a, None):
            lines.append(f"{a}={name(getattr(e, a))}")
    if members:
        ps = await c.get_participants(e, limit=members)
        lines.append(f"участников всего: {ps.total}")
        lines += [f"  {p.id} | {person(p)} | @{p.username or '-'}" for p in ps]
    return "\n".join(lines)


@tool()
async def list_contacts(limit: int = 100) -> str:
    """Контакты из телефонной книги Telegram."""
    c = await tg()
    r = await c(functions.contacts.GetContactsRequest(hash=0))
    return "\n".join(f"{u.id} | {person(u)} | @{u.username or '-'}" for u in r.users[:limit]) or "Контактов нет."


@tool()
async def get_common_chats(user: str) -> str:
    """Общие группы и каналы с пользователем."""
    c, ent = await peer(user)
    r = await c(functions.messages.GetCommonChatsRequest(user_id=ent, max_id=0, limit=100))
    return "\n".join(f"{ch.id} | {name(ch.title)}" for ch in r.chats) or "Общих чатов нет."


@tool()
async def get_profile() -> str:
    """Мой профиль Telegram: имя, username, телефон, «о себе»."""
    c = await tg()
    full = await c(functions.users.GetFullUserRequest("me"))
    u = full.users[0]
    return (f"id={u.id}\nимя={u.first_name or ''} {u.last_name or ''}\nusername=@{u.username or '-'}\n"
            f"телефон=+{u.phone}\nо себе={full.full_user.about or ''}")


PRIVACY_KEYS = {
    "last_seen": types.InputPrivacyKeyStatusTimestamp, "phone": types.InputPrivacyKeyPhoneNumber,
    "photo": types.InputPrivacyKeyProfilePhoto, "forwards": types.InputPrivacyKeyForwards,
    "calls": types.InputPrivacyKeyPhoneCall, "groups": types.InputPrivacyKeyChatInvite,
    "bio": types.InputPrivacyKeyAbout,
}
PRIVACY_RULES = {
    "everybody": [types.InputPrivacyValueAllowAll()],
    "contacts": [types.InputPrivacyValueAllowContacts(), types.InputPrivacyValueDisallowAll()],
    "nobody": [types.InputPrivacyValueDisallowAll()],
}


@tool()
async def get_privacy(setting: str) -> str:
    """Настройка приватности: last_seen, phone, photo, forwards, calls, groups, bio."""
    c = await tg()
    r = await c(functions.account.GetPrivacyRequest(key=PRIVACY_KEYS[setting]()))
    return ", ".join(type(x).__name__.replace("PrivacyValue", "") for x in r.rules) or "нет правил"


# ---------- отправка ----------

@tool(write=True)
async def send_message(chat: str, text: str, reply_to: int = 0, schedule_minutes: int = 0) -> str:
    """Отправить сообщение от имени владельца. reply_to — id сообщения для ответа; schedule_minutes>0 — отложить."""
    err = send_guard("tg", chat, text)
    if err:
        return err
    c, ent = await peer(chat)
    m = await c.send_message(ent, text, reply_to=reply_to or None,
                             schedule=timedelta(minutes=schedule_minutes) if schedule_minutes else None)
    mark_sent("tg", chat, m.id, f"len={len(text)}")
    return f"{'Запланировано' if schedule_minutes else 'Отправлено'} (id {m.id})."


@tool(write=True, meta=media.FILE_META)
async def send_file(chat: str, file: str | dict, caption: str = "", as_document: bool = False) -> str:
    """Отправить картинку/видео/файл: file — ссылка http(s) или файл, прикреплённый в чате ChatGPT."""
    err = send_guard("tg", chat)
    if err:
        return err
    data, fname, _ = await media.fetch(file)
    c, ent = await peer(chat)
    m = await c.send_file(ent, as_file(data, fname), caption=caption or None, force_document=as_document)
    mark_sent("tg", chat, m.id, f"file={fname}")
    return f"Отправлено (id {m.id})."


@tool(write=True)
async def send_album(chat: str, files: list[str], caption: str = "") -> str:
    """Отправить альбом (2–10 картинок/видео по ссылкам http(s)) одним сообщением."""
    err = send_guard("tg", chat)
    if err:
        return err
    if not 2 <= len(files) <= 10:
        return "Отказ: в альбоме от 2 до 10 файлов."
    got = [await media.fetch(f) for f in files]
    c, ent = await peer(chat)
    ms = await c.send_file(ent, [as_file(d, n) for d, n, _ in got], caption=caption or None)
    mark_sent("tg", chat, ",".join(str(m.id) for m in ms), f"album={len(ms)}")
    return f"Альбом отправлен ({len(ms)} шт.)."


@tool(write=True)
async def send_poll(chat: str, question: str, options: list[str], multiple: bool = False,
                    anonymous: bool = True, quiz_correct: int = -1) -> str:
    """Создать опрос (2–10 вариантов). quiz_correct — номер верного ответа с 0 для викторины."""
    err = send_guard("tg", chat, question)
    if err:
        return err
    if not 2 <= len(options) <= 10:
        return "Отказ: нужно от 2 до 10 вариантов."
    quiz = quiz_correct >= 0
    poll = types.Poll(
        id=random.getrandbits(62), hash=0,
        question=types.TextWithEntities(question, []),
        answers=[types.PollAnswer(types.TextWithEntities(o, []), bytes([i])) for i, o in enumerate(options)],
        multiple_choice=multiple and not quiz, quiz=quiz, public_voters=not anonymous,
    )
    c, ent = await peer(chat)
    m = await c.send_file(ent, types.InputMediaPoll(poll, correct_answers=[quiz_correct] if quiz else None))
    mark_sent("tg", chat, m.id, "poll")
    return f"Опрос отправлен (id {m.id})."


@tool(write=True)
async def vote_poll(chat: str, message_id: int, options: list[int]) -> str:
    """Проголосовать в опросе: номера вариантов с 0 (пустой список — отозвать голос)."""
    c, ent = await peer(chat)
    m = await c.get_messages(ent, ids=message_id)
    if not m or not m.poll:
        return "Это не опрос."
    answers = m.poll.poll.answers
    await c(functions.messages.SendVoteRequest(peer=ent, msg_id=message_id,
                                               options=[answers[i].option for i in options]))
    return "Голос учтён." if options else "Голос отозван."


@tool(write=True)
async def press_button(chat: str, message_id: int, index: int) -> str:
    """Нажать инлайн-кнопку сообщения (номер из list_buttons)."""
    c, ent = await peer(chat)
    m = await c.get_messages(ent, ids=message_id)
    r = await m.click(index)
    return f"Нажато. Ответ бота: {clean(getattr(r, 'message', None) or '', 500) or 'нет'}"


@tool(write=True)
async def forward_messages(from_chat: str, message_ids: list[int], to_chat: str) -> str:
    """Переслать сообщения из одного чата в другой."""
    err = send_guard("tg", to_chat)
    if err:
        return err
    c, src = await peer(from_chat)
    ms = await c.forward_messages(await resolve(c, norm(to_chat)), message_ids, src)
    ms = ms if isinstance(ms, list) else [ms]
    mark_sent("tg", to_chat, ",".join(str(m.id) for m in ms if m), "forward")
    return f"Переслано: {len(ms)}."


# ---------- управление сообщениями и чатами ----------

@tool(write=True)
async def edit_message(chat: str, message_id: int, text: str) -> str:
    """Изменить текст своего сообщения."""
    c, ent = await peer(chat)
    await c.edit_message(ent, message_id, text)
    return "Изменено."


@tool(write=True, destructive=True)
async def delete_messages(chat: str, message_ids: list[int], for_everyone: bool = True) -> str:
    """Удалить сообщения (for_everyone=false — только у себя)."""
    c, ent = await peer(chat)
    r = await c.delete_messages(ent, message_ids, revoke=for_everyone)
    print(f"TG DELETE chat={chat} ids={message_ids}", file=sys.stderr, flush=True)
    return f"Удалено: {sum(x.pts_count for x in r)}."


@tool(write=True)
async def pin_message(chat: str, message_id: int, unpin: bool = False) -> str:
    """Закрепить или открепить сообщение."""
    c, ent = await peer(chat)
    await (c.unpin_message(ent, message_id) if unpin else c.pin_message(ent, message_id))
    return "Откреплено." if unpin else "Закреплено."


@tool(write=True)
async def react(chat: str, message_id: int, emoji: str = "👍") -> str:
    """Поставить реакцию на сообщение (пустая строка — убрать)."""
    c, ent = await peer(chat)
    await c(functions.messages.SendReactionRequest(
        peer=ent, msg_id=message_id, reaction=[types.ReactionEmoji(emoticon=emoji)] if emoji else []))
    return "Готово."


@tool(write=True)
async def mark_read(chat: str) -> str:
    """Отметить чат прочитанным."""
    c, ent = await peer(chat)
    await c.send_read_acknowledge(ent)
    return "Прочитано."


@tool(write=True)
async def archive_chat(chat: str, archived: bool = True) -> str:
    """Убрать чат в архив или вернуть из архива (archived=false)."""
    c, ent = await peer(chat)
    await c.edit_folder(ent, 1 if archived else 0)
    return "В архиве." if archived else "Возвращён из архива."


@tool(write=True)
async def mute_chat(chat: str, minutes: int = 0) -> str:
    """Выключить уведомления чата на minutes минут (0 — навсегда, -1 — включить обратно)."""
    c, ent = await peer(chat)
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes) if minutes > 0
             else datetime(2038, 1, 1, tzinfo=timezone.utc) if minutes == 0 else datetime.fromtimestamp(0, timezone.utc))
    await c(functions.account.UpdateNotifySettingsRequest(
        peer=types.InputNotifyPeer(ent), settings=types.InputPeerNotifySettings(mute_until=until)))
    return "Уведомления включены." if minutes < 0 else "Уведомления выключены."


@tool(write=True)
async def block_user(user: str, unblock: bool = False) -> str:
    """Заблокировать или разблокировать пользователя."""
    c, ent = await peer(user)
    await c(functions.contacts.UnblockRequest(id=ent) if unblock else functions.contacts.BlockRequest(id=ent))
    return "Разблокирован." if unblock else "Заблокирован."


@tool(write=True)
async def create_group(title: str, users: list[str]) -> str:
    """Создать группу с пользователями (id или @username)."""
    c = await tg()
    r = await c(functions.messages.CreateChatRequest(
        users=[await resolve(c, norm(u)) for u in users], title=title))
    chat = getattr(r, "updates", r).chats[0]
    return f"Группа создана: id=-{chat.id}"


@tool(write=True)
async def invite_to_group(chat: str, users: list[str]) -> str:
    """Добавить пользователей в группу или канал."""
    c, ent = await peer(chat)
    us = [await resolve(c, norm(u)) for u in users]
    if isinstance(ent, types.InputPeerChannel):
        await c(functions.channels.InviteToChannelRequest(channel=ent, users=us))
    else:
        for u in us:
            await c(functions.messages.AddChatUserRequest(chat_id=ent.chat_id, user_id=u, fwd_limit=50))
    return f"Добавлено: {len(us)}."


@tool(write=True)
async def set_admin(chat: str, user: str, admin: bool = True, title: str = "") -> str:
    """Назначить или снять администратора группы/канала."""
    c, ent = await peer(chat)
    u = await resolve(c, norm(user))
    rights = dict(change_info=admin, delete_messages=admin, ban_users=admin, invite_users=admin,
                  pin_messages=admin, manage_call=admin)
    if isinstance(ent, types.InputPeerChannel):
        await c.edit_admin(ent, u, title=title or None, **rights)
    else:
        await c.edit_admin(ent, u, is_admin=admin)
    return "Назначен администратором." if admin else "Права администратора сняты."


@tool(write=True, destructive=True)
async def kick_user(chat: str, user: str) -> str:
    """Удалить пользователя из группы."""
    c, ent = await peer(chat)
    await c.kick_participant(ent, await resolve(c, norm(user)))
    return "Удалён из группы."


@tool(write=True, meta={"openai/fileParams": ["photo"]})
async def edit_chat(chat: str, title: str = "", photo: str | dict = "") -> str:
    """Изменить название и/или фото группы/канала (photo — ссылка или файл из чата ChatGPT)."""
    c, ent = await peer(chat)
    channel = isinstance(ent, types.InputPeerChannel)
    if title:
        await c(functions.channels.EditTitleRequest(ent, title) if channel
                else functions.messages.EditChatTitleRequest(ent.chat_id, title))
    if photo:
        data, fname, _ = await media.fetch(photo)
        up = types.InputChatUploadedPhoto(file=await c.upload_file(data, file_name=fname))
        await c(functions.channels.EditPhotoRequest(ent, up) if channel
                else functions.messages.EditChatPhotoRequest(ent.chat_id, up))
    return "Чат обновлён."


@tool(write=True, destructive=True)
async def leave_chat(chat: str) -> str:
    """Выйти из группы/канала (для личного чата — удалить диалог у себя)."""
    c, ent = await peer(chat)
    await c.delete_dialog(ent)
    return "Готово."


# ---------- профиль ----------

@tool(write=True)
async def update_profile(first_name: str = "", last_name: str = "", about: str = "") -> str:
    """Изменить имя, фамилию или «о себе» (пустые поля не меняются)."""
    c = await tg()
    await c(functions.account.UpdateProfileRequest(
        first_name=first_name or None, last_name=last_name or None, about=about or None))
    return "Профиль обновлён."


@tool(write=True, meta=media.FILE_META)
async def set_avatar(file: str | dict) -> str:
    """Поставить новую аватарку: ссылка на картинку или файл, прикреплённый в чате ChatGPT."""
    data, fname, _ = await media.fetch(file)
    c = await tg()
    await c(functions.photos.UploadProfilePhotoRequest(file=await c.upload_file(data, file_name=fname)))
    return "Аватарка обновлена."


@tool(write=True)
async def set_privacy(setting: str, value: str) -> str:
    """Приватность: setting = last_seen|phone|photo|forwards|calls|groups|bio, value = everybody|contacts|nobody."""
    c = await tg()
    await c(functions.account.SetPrivacyRequest(key=PRIVACY_KEYS[setting](), rules=PRIVACY_RULES[value]))
    return f"{setting}: {value}."


try:  # MAX необязателен: без pymax сервер работает только с Telegram
    import max_tools
except ImportError:
    max_tools = None
max_on = bool(max_tools and os.environ.get("MAX_ENABLED", "1") != "0")
if max_on:
    max_tools.register(tool, send_guard, mark_sent, Image)


# ---------- диалоги: привязка чата к цели, ожидание ответа ----------

async def _tg_incoming(event):
    """Входящее сообщение: интересует только привязанный чат (иначе лишний запрос за именем отправителя не нужен)."""
    if not dialogs.watching("tg", event.chat_id):
        return
    m = event.message
    s = await event.get_sender()
    await dialogs.push("tg", event.chat_id, m.id, m.date.timestamp(), person(s) if s is not None else str(m.sender_id),
                       " ".join(x for x in (kind(m), m.text or "") if x))


async def _tg_resolve(chat):
    c, ent = await peer(chat)
    return utils.get_peer_id(ent), person(await c.get_entity(ent))


async def _tg_context(chat_id):
    c = await tg()
    ent = await c.get_input_entity(chat_id)
    return [fmt(m, 500) async for m in c.iter_messages(ent, limit=8)][::-1]


async def _tg_ensure():
    await tg()


if max_on:
    max_tools.message_hook = dialogs.max_message_hook
max_hooks = dict(resolve=max_tools.dialog_resolve, context=max_tools.dialog_context, ensure=max_tools.mx) if max_on else None
dialogs.register(tool, dict(resolve=_tg_resolve, context=_tg_context, ensure=_tg_ensure), max_hooks)


class DebugLog:
    """TG_DEBUG=1: пишет в журнал заголовки запросов и ответы с ошибкой.

    Тело запроса не пишется: в нём аргументы инструментов, то есть тексты сообщений.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        hdrs = {k.decode(): v.decode() for k, v in scope["headers"] if k not in (b"cookie", b"authorization")}
        print(f"DEBUG {scope['method']} headers={hdrs}", file=sys.stderr, flush=True)
        status = {}

        async def snd(m):
            if m["type"] == "http.response.start":
                status["code"] = m["status"]
            elif m["type"] == "http.response.body" and status.get("code", 200) >= 400:
                print(f"DEBUG {status['code']} body={m.get('body', b'')[:400]!r}", file=sys.stderr, flush=True)
            await send(m)

        await self.app(scope, receive, snd)


class Boot:
    """При запуске поднимает сохранённые привязки диалогов и держит подключения к мессенджерам."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            async def recv():
                m = await receive()
                if m["type"] == "lifespan.startup":
                    dialogs.start()
                return m
            return await self.app(scope, recv, send)
        return await self.app(scope, receive, send)


if __name__ == "__main__":
    if STDIO:
        mcp.run("stdio")
    else:
        import uvicorn
        app = mcp.streamable_http_app(host=HOST, **HTTP_OPTS)
        # access_log выключен: в пути лежит секрет (/<TG_SECRET>/mcp)
        uvicorn.run(Boot(DebugLog(app) if os.environ.get("TG_DEBUG") else app), host=HOST, port=PORT, access_log=False)
