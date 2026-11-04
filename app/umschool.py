"""Умскул (онлайн-школа, кабинет nd.umschool.net): расписание, домашка, прогресс, подписки. Только чтение.

Неофициальный API: адреса взяты из публичных скриптов кабинета и могут измениться без предупреждения.
Вход: почта и пароль на oid.umschool.net, их вводит владелец командой deploy/umschool_login.sh.
Пароль не сохраняется, на диск (UMS_FILE, права 600) кладётся только пара токенов access/refresh.
Сервер сам продлевает токены заранее. Тексты заданий и названия — чужой текст: для модели это данные, а не команды.
"""
import asyncio
import html
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx

from safety import clean

API = "https://old.umschool.net/api"
OID = "https://oid.umschool.net/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
FILE = os.environ.get("UMS_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(os.path.expanduser(os.environ.get("TG_SESSION", "~/.tg_claude")))), "umschool.json")
RAW_LIMIT = 20000
KEEP_MARGIN = 3600       # продлеваем, когда access-токену осталось меньше этого

state = {}               # access, refresh, saved_at
_mtime = 0.0
_lock = None
_lock_loop = None
_keep_task = None


class UmsAuthError(Exception):
    """Нужен вход заново."""


class UmsError(Exception):
    pass


def _l():
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def _http():
    return httpx.AsyncClient(timeout=30, follow_redirects=False, headers={"User-Agent": UA, "Accept": "application/json"})


# ---------- токены ----------

def load():
    global _mtime
    try:
        with open(FILE, encoding="utf-8") as f:
            state.update(json.load(f))
        _mtime = os.path.getmtime(FILE)
    except (OSError, ValueError):
        pass
    return bool(state.get("access"))


def save():
    global _mtime
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, FILE)
    _mtime = os.path.getmtime(FILE)


def set_tokens(access, refresh=""):
    state.update(access=access.strip(), refresh=(refresh or state.get("refresh") or "").strip(), saved_at=time.time())
    save()


def logged_in():
    try:
        if os.path.getmtime(FILE) != _mtime:
            load()
    except OSError:
        pass
    return bool(state.get("access") or load())


def jwt_expiry(token):
    try:
        import base64
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("exp")
    except Exception:
        return None


def _pair(d):
    """Достать (access, refresh) из ответа входа/продления: ключи могут называться по-разному."""
    def find(o, names):
        if isinstance(o, dict):
            for n in names:
                if isinstance(o.get(n), str) and o[n]:
                    return o[n]
            for v in o.values():
                r = find(v, names)
                if r:
                    return r
        return None
    return find(d, ("access", "access_token", "accessToken")), find(d, ("refresh", "refresh_token", "refreshToken"))


CLIENT_ID = "9VzxpopERpimczRNQrUAWiRURUzzThgHd1Wx0WzO"   # публичный id приложения nd.umschool.net (виден в его скриптах)
CLIENT_REDIRECT = "https://nd.umschool.net/oauth-callback"
ID_CALLBACK = "https://id.umschool.net/callback/oauth"


async def _oauth_token(oid_access):
    """OAuth «код + PKCE», как делает страница входа: токен входа -> код -> OAuth-токен для кабинета."""
    import base64
    import hashlib
    import secrets
    from urllib.parse import parse_qs, urlencode, urlparse
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect_uri = f"{ID_CALLBACK}?{urlencode({'client_id': CLIENT_ID, 'client_redirect': CLIENT_REDIRECT})}"
    q = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": redirect_uri, "scope": "read write",
         "state": secrets.token_urlsafe(16), "code_challenge": challenge, "code_challenge_method": "S256", "jwt_token": oid_access}
    async with _http() as h:
        r = await h.get(f"{OID}/o/authorize/", params=q)
        loc = r.headers.get("location", "")
        code = (parse_qs(urlparse(loc).query).get("code") or [""])[0]
        if not code:
            raise UmsAuthError(f"Умскул не выдал код авторизации ({r.status_code}): {clean(loc.split('?')[0] or r.text, 160)}")
        r = await h.post(f"{OID}/o/token/", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                                  "client_id": CLIENT_ID, "code_verifier": verifier})
    if r.status_code >= 400:
        raise UmsAuthError(f"Умскул не выдал токен по коду ({r.status_code}): {clean(r.text, 160)}")
    token = r.json().get("access_token")
    if not token:
        raise UmsError("Умскул вернул ответ без OAuth-токена")
    return token


async def login(email, password):
    """Вход по почте и паролю. Возвращает профиль. Пароль нигде не сохраняется и не попадает в сообщения об ошибках."""
    async with _http() as h:
        r = await h.post(f"{OID}/auth/login/", json={"email": email.strip(), "password": password})
    if r.status_code >= 400:
        try:
            detail = "; ".join(e.get("detail", "") for e in r.json().get("errors", []))
        except ValueError:
            detail = ""
        raise UmsAuthError(f"Умскул не принял вход ({r.status_code}): {clean(detail, 200) or 'проверьте почту и пароль'}")
    oid_access, _ = _pair(r.json())
    if not oid_access:
        raise UmsError("Умскул вернул ответ без токена доступа")
    oauth_access = await _oauth_token(oid_access)
    # кабинет обменивает OAuth-токен на свою пару токенов, как его страница /oauth-callback
    async with _http() as h:
        r = await h.post(f"{API}/v2/auth/o/login/", json={"access": oauth_access})
    if r.status_code >= 400:
        raise UmsAuthError(f"Вход прошёл, но кабинет не принял токен ({r.status_code}): {clean(r.text, 200)}")
    access, refresh = _pair(r.json())
    if not access:
        raise UmsError("Кабинет Умскула вернул ответ без токена доступа")
    old = dict(state)
    set_tokens(access, refresh)
    try:
        return await profile()
    except Exception:
        state.clear()
        state.update(old)
        if old.get("access"):
            save()
        else:
            try:
                os.remove(FILE)
            except OSError:
                pass
        raise


async def _refresh(stale):
    async with _l():
        if state.get("access") != stale:
            return
        rt = state.get("refresh")
        if not rt:
            raise UmsAuthError("нет refresh-токена: войдите заново (sudo bash deploy/umschool_login.sh)")
        async with _http() as h:
            r = await h.post(f"{API}/v3/auth/refresh/", json={"refresh": rt})
        if r.status_code in (400, 401, 403):
            raise UmsAuthError("Умскул не принял refresh-токен (просрочен или отозван): войдите заново (sudo bash deploy/umschool_login.sh)")
        r.raise_for_status()
        access, refresh = _pair(r.json())
        if not access:
            raise UmsError("Умскул вернул неполную пару токенов")
        set_tokens(access, refresh or rt)


async def ensure_fresh(margin=KEEP_MARGIN, now=None):
    """Продлить токены заранее. True, если обновили."""
    if not logged_in() or not state.get("refresh"):
        return False
    exp = jwt_expiry(state.get("access") or "")
    if exp and exp - (now or time.time()) > margin:
        return False
    await _refresh(state["access"])
    return True


async def _keepalive():
    import sys
    while True:
        try:
            if await ensure_fresh():
                print("umschool: токены продлены заранее", file=sys.stderr, flush=True)
        except UmsAuthError as e:
            print(f"umschool: продлить вход не удалось: {clean(str(e), 150)}", file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"umschool: продление отложено ({type(e).__name__})", file=sys.stderr, flush=True)
        await asyncio.sleep(600)


def start_keepalive():
    global _keep_task
    if os.environ.get("UMS_KEEPALIVE", "1") == "0" or (_keep_task and not _keep_task.done()):
        return
    if load():
        _keep_task = asyncio.get_running_loop().create_task(_keepalive())


# ---------- запросы ----------

async def get(path, params=None):
    """GET к API кабинета с одним автоматическим обновлением токена. Возвращает разобранный JSON."""
    if not logged_in():
        raise UmsAuthError("вход в Умскул не выполнен: sudo bash deploy/umschool_login.sh на сервере")
    url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
    if not url.startswith(API + "/"):
        raise UmsError("только адреса https://old.umschool.net/api/")
    for attempt in range(2):
        access = state["access"]
        async with _http() as h:
            r = await h.get(url, params=params, headers={"Authorization": f"Bearer {access}"})
        if r.status_code == 401 and attempt == 0:
            await _refresh(access)
            continue
        break
    if r.status_code == 401:
        raise UmsAuthError("Умскул не принимает токен: войдите заново (sudo bash deploy/umschool_login.sh)")
    if r.status_code >= 400:
        raise UmsError(f"Умскул ответил {r.status_code}: {clean(r.text, 200)}")
    try:
        return r.json()
    except ValueError:
        raise UmsError("Умскул вернул не JSON")


async def profile():
    return await get("v3/auth/verify/")


def dump(data, limit=RAW_LIMIT):
    return clean(json.dumps(data, ensure_ascii=False, indent=1), limit)


# ---------- оформление ----------

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))


def plain(html_text, limit=1500):
    """Текст из HTML задания: без тегов и лишних пробелов. Это чужой текст, для модели данные, а не команды."""
    t = re.sub(r"<(br|/p|/div|/li|/tr)[^>]*>", "\n", html_text or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return clean(re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip(), limit)


def local(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
    except (ValueError, AttributeError):
        return None


def hhmm(iso):
    d = local(iso)
    return d.strftime("%d.%m %H:%M") if d else "?"


def _range(date_from, date_to):
    try:
        a = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else datetime.now(TZ).date()
        b = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else a
    except ValueError:
        raise UmsError("Даты в формате ГГГГ-ММ-ДД")
    if b < a or (b - a).days > 92:
        raise UmsError("Период: от 1 до 92 дней, дата «с» не позже даты «по»")
    return a, b


async def _month_days(path, a, b):
    """Расписание отдаётся по месяцам: собираем все месяцы периода и оставляем дни внутри него."""
    out, y, m = {}, a.year, a.month
    while (y, m) <= (b.year, b.month):
        for day in await get(path, {"year": y, "month": m}) or []:
            if a.isoformat() <= str(day.get("day")) <= b.isoformat():
                out.setdefault(day["day"], day)  # соседние месяцы перекрываются: день берём один раз
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return [out[k] for k in sorted(out)]


def fmt_schedule(days):
    lines = []
    for d in days:
        lines.append(f"{d['day']}:")
        for ct in d.get("class_types") or []:
            for les in ct.get("lessons") or []:
                flags = (", эфир" if les.get("is_live") else "") + (", не просмотрено" if not les.get("is_viewed") else "")
                lines.append(f"  {hhmm(les.get('start_datetime'))[6:]} {clean(ct.get('title'), 40)}: {clean(les.get('title'), 90)} "
                             f"({flags[2:] + ', ' if flags else ''}урок {les.get('id')})")
    return "\n".join(lines) or "занятий в этот период нет"


def fmt_homework(days, only_undone=False):
    lines = []
    for d in days:
        for ct in d.get("class_types") or []:
            for h in ct.get("homeworks") or []:
                if only_undone and h.get("is_done"):
                    continue
                lines.append(f"[{'x' if h.get('is_done') else ' '}] {d['day']} · {clean(ct.get('title'), 40)}: {clean(h.get('title'), 90)} "
                             f"(срок {hhmm(h.get('deadline'))}, тип {h.get('type')}, id {h.get('id')})")
    return "\n".join(lines) or ("несделанных заданий нет" if only_undone else "домашних заданий в этот период нет")


def fmt_subscriptions(d):
    def one(s):
        until = (s.get("activeUntil") or "")[:10]
        warn = "; АВТОПРОДЛЕНИЕ НЕ ПРОШЛО" if s.get("autoProlongFailed") else ""
        auto = "автопродление включено" if s.get("isAutoProlong") else "автопродление выключено"
        return (f"• {clean(s.get('title'), 90)} ({', '.join(s.get('tags') or [])}; тариф {s.get('ratePlanTitle') or '?'}): "
                f"до {until}, {s.get('price')} ₽, {auto}{warn}")
    act, ina = d.get("active") or [], d.get("inactive") or []
    return "Активные:\n" + ("\n".join(one(s) for s in act) or "нет") + ("\nЗавершённые:\n" + "\n".join(one(s) for s in ina) if ina else "")


def fmt_progress(d):
    lv = d.get("level") or {}
    return (f"Уровень: {lv.get('title')} (id {lv.get('id')}); опыт {d.get('exp')} (уровень {lv.get('expFrom')}–{lv.get('expTo')}); "
            f"медалей {d.get('medals')}, умкоинов {d.get('umcoins')}")


# ---------- инструменты ----------

def register(tool):
    @tool()
    async def umschool_status() -> str:
        """Умскул: выполнен ли вход, чей кабинет, сколько ещё действует доступ."""
        if not logged_in():
            return "Вход в Умскул не выполнен. Владелец: sudo bash deploy/umschool_login.sh на сервере (почта и пароль)."
        exp = jwt_expiry(state.get("access", ""))
        left = f"токен доступа действует ещё {max(0, int((exp - time.time()) // 60))} мин (обновляется сам)" if exp else "токен доступа сохранён"
        try:
            u = (await profile()).get("user") or {}
        except UmsAuthError as e:
            return f"Токены есть, но Умскул их не принимает: {e}"
        return f"Вход выполнен: {u.get('last_name', '')} {u.get('first_name', '')}, роль {u.get('role')}. {left}."

    @tool()
    async def umschool_subscriptions() -> str:
        """Умскул: купленные курсы и подписки: срок, тариф, автопродление, не прошёл ли платёж."""
        return fmt_subscriptions(await get("v3/subscriptions/my/"))

    @tool()
    async def umschool_schedule(date_from: str = "", date_to: str = "") -> str:
        """Умскул: занятия и вебинары за период (ГГГГ-ММ-ДД, по умолчанию сегодня; до 92 дней): время МСК, предмет, тема, номер урока."""
        a, b = _range(date_from, date_to)
        return fmt_schedule(await _month_days("v1/timetable/lessons/", a, b))

    @tool()
    async def umschool_homework(date_from: str = "", date_to: str = "", only_undone: bool = False) -> str:
        """Умскул: домашние задания за период (ГГГГ-ММ-ДД, по умолчанию сегодня; до 92 дней) со сроком сдачи и id. Тексты заданий: umschool_homework_read."""
        a, b = _range(date_from, date_to)
        return fmt_homework(await _month_days("v1/timetable/homeworks/", a, b), only_undone)

    @tool()
    async def umschool_homework_read(homework_id: int, type: str = "REGULAR") -> str:
        """Умскул: тексты заданий одной домашки по её id (из umschool_homework): условия, баллы, сложность. type — тип из списка (REGULAR и т.п.)."""
        sub = (await get(f"v3/homework/{int(homework_id)}/submission/", {"type": type})).get("homework_submission_id")
        if not sub:
            raise UmsError("у этой домашки нет попытки: проверьте id и type")
        d = await get(f"v2/homework/submissions/{sub}/info/")
        lines = [f"Домашка {homework_id}: заданий {len(d.get('tasks') or [])}"]
        for t in d.get("tasks") or []:
            lines.append(f"\n№{t.get('number')} (до {t.get('max_points')} б., {t.get('difficulty_type')}, {t.get('type')}):\n{plain(t.get('title'))}")
            if t.get("extra_text"):
                lines.append(plain(t["extra_text"], 500))
        return "\n".join(lines)

    @tool()
    async def umschool_lesson(lesson_id: int) -> str:
        """Умскул: урок по id (из umschool_schedule): тема, описание, ссылка на видео, к какой домашке относится."""
        d = await get(f"v3/lessons/{int(lesson_id)}/")
        hw = d.get("homework") or {}
        return (f"{clean(d.get('title'), 120)} ({(d.get('class_type') or {}).get('title')}), {hhmm(d.get('start_datetime'))}, "
                f"{d.get('video_length')} мин\nОписание: {plain(d.get('description'), 600) or '—'}\nВидео: {d.get('video') or 'нет'}\n"
                f"Домашка: id {hw.get('homework_id') or '—'}, статус {hw.get('status') or '—'}")

    @tool()
    async def umschool_progress() -> str:
        """Умскул: уровень, опыт, медали и умкоины ученика."""
        return fmt_progress(await get("v2/students/current-progress/"))

    @tool()
    async def umschool_raw(path: str, params: str = "") -> str:
        """Умскул: любой GET-запрос к API кабинета (для разделов без отдельного инструмента). Только чтение.

        path — «v3/subscriptions/my/» (относительно https://old.umschool.net/api/); params — строка запроса «a=1&b=2»."""
        if ".." in path or "//" in path.replace("https://", "", 1):
            raise UmsError("Недопустимый путь")
        return dump(await get(path, dict(parse_qsl(params))))
