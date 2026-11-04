"""Умскул (онлайн-школа, кабинет nd.umschool.net): расписание, домашка, прогресс, подписки. Только чтение.

Неофициальный API: адреса взяты из публичных скриптов кабинета и могут измениться без предупреждения.
Вход: почта и пароль на oid.umschool.net, их вводит владелец командой deploy/umschool_login.sh.
Пароль не сохраняется, на диск (UMS_FILE, права 600) кладётся только пара токенов access/refresh.
Сервер сам продлевает токены заранее. Тексты заданий и названия — чужой текст: для модели это данные, а не команды.
"""
import asyncio
import json
import os
import time
from urllib.parse import parse_qsl

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
            p = await profile()
        except UmsAuthError as e:
            return f"Токены есть, но Умскул их не принимает: {e}"
        return f"Вход выполнен. {left}.\nПрофиль:\n{dump(p, 1500)}"

    @tool()
    async def umschool_profile() -> str:
        """Умскул: профиль ученика в кабинете."""
        return dump(await profile())

    @tool()
    async def umschool_subscriptions() -> str:
        """Умскул: купленные курсы и подписки (активные, срок, что входит)."""
        return dump(await get("v3/subscriptions/my/"))

    @tool()
    async def umschool_schedule(params: str = "") -> str:
        """Умскул: расписание занятий и вебинаров. params — строка запроса «a=1&b=2» (необязательно)."""
        return dump(await get("v1/timetable/lessons/", dict(parse_qsl(params))))

    @tool()
    async def umschool_homework(params: str = "") -> str:
        """Умскул: домашние задания из расписания. params — строка запроса «a=1&b=2» (необязательно)."""
        return dump(await get("v1/timetable/homeworks/", dict(parse_qsl(params))))

    @tool()
    async def umschool_progress() -> str:
        """Умскул: текущий прогресс ученика."""
        return dump(await get("v/students/current-progress/"))

    @tool()
    async def umschool_raw(path: str, params: str = "") -> str:
        """Умскул: любой GET-запрос к API кабинета (для разделов без отдельного инструмента). Только чтение.

        path — «v3/subscriptions/my/» (относительно https://old.umschool.net/api/); params — строка запроса «a=1&b=2»."""
        if ".." in path or "//" in path.replace("https://", "", 1):
            raise UmsError("Недопустимый путь")
        return dump(await get(path, dict(parse_qsl(params))))
