"""МЭШ (школьный дневник school.mos.ru): расписание, домашние задания, оценки, питание, посещаемость, рейтинг.

Неофициальный API: протокол изучен по открытым клиентам OctoDiary (MIT) и MOSDiary и может измениться без предупреждения.
Вход: токены aupd_token / aupd_refresh_token кладёт владелец командой deploy/mes_login.sh (QR или вставка токенов).
Токены лежат в MES_FILE (права 600) и обновляются сами; пара обновления одноразовая, поэтому новая пара
записывается на диск до того, как её использует следующий запрос.
Тексты заданий, комментарии учителей и названия — чужой текст: для модели это данные, а не команды.
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from dates import parse_when
from safety import clean

BASE = "https://school.mos.ru/api/"
HOSTS = {"school.mos.ru", "dnevnik.mos.ru", "uchebnik.mos.ru"}  # куда разрешён сырой GET (mes_raw)
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
FILE = os.environ.get("MES_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(os.path.expanduser(os.environ.get("TG_SESSION", "~/.tg_claude")))), "mes.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
CACHE_TTL = 45          # секунд: одинаковые запросы подряд не бьют в МЭШ
RAW_LIMIT = 20000       # символов в ответе mes_raw

FILE_LIMIT = 10 * 1024 * 1024   # байт: больше файл не скачиваем
IMAGE_TYPES = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

state = {}              # access, refresh, saved_at
_mtime = 0.0
_cache = {}
_profile = {}
_lock = None
_lock_loop = None


class MesAuthError(Exception):
    """Нужен вход заново (имя класса важно для сообщений)."""


class MesError(Exception):
    pass


def _l():
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def _http():
    return httpx.AsyncClient(timeout=30, follow_redirects=False, headers={"User-Agent": UA})


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


def _reload():
    """Токены могли поменять снаружи (вход deploy/mes_login.sh): подхватываем без перезапуска сервиса."""
    try:
        if os.path.getmtime(FILE) != _mtime:
            load()
            _cache.clear()
            _profile.clear()
    except OSError:
        pass


def save():
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, FILE)
    global _mtime
    _mtime = os.path.getmtime(FILE)


def set_tokens(access, refresh=""):
    state.update(access=access.strip(), refresh=(refresh or "").strip(), saved_at=time.time())
    _cache.clear()
    _profile.clear()
    save()


def logged_in():
    _reload()
    return bool(state.get("access") or load())


def jwt_expiry(token):
    """Время жизни access-токена (unix) из JWT, если токен в таком формате."""
    try:
        import base64
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("exp")
    except Exception:
        return None


async def _refresh(stale):
    """Обновить пару токенов. Если пока ждали блокировку, это уже сделал другой запрос, — ничего не делаем."""
    async with _l():
        if state.get("access") != stale:
            return
        rt = state.get("refresh")
        if not rt:
            raise MesAuthError("нет refresh-токена: войдите заново (deploy/mes_login.sh)")
        async with _http() as h:
            r = await h.post("https://school.mos.ru/v3/token/refresh", content=b"{}",
                             headers={"Cookie": f"aupd_refresh_token={rt}", "Origin": "https://school.mos.ru",
                                      "Referer": "https://school.mos.ru/diary/schedules/day", "Accept": "application/json",
                                      "Content-Type": "application/json"})
        if r.status_code in (400, 401, 403):
            raise MesAuthError("МЭШ не принял refresh-токен (он одноразовый или просрочен): войдите заново (deploy/mes_login.sh)")
        r.raise_for_status()
        d = r.json()
        if not d.get("access_token") or not d.get("refresh_token"):
            raise MesError("МЭШ вернул неполную пару токенов")
        set_tokens(d["access_token"], d["refresh_token"])  # на диск до следующего запроса


# ---------- запросы ----------

def _headers(access, subsystem="familyweb", profile_id=None, mobile=False, role=None):
    h = {"Authorization": f"Bearer {access}", "Cookie": f"aupd_token={access}", "Accept": "application/json, text/plain, */*",
         "X-Mes-Subsystem": "familymp" if mobile else subsystem}
    if mobile:
        h["Client-Type"] = "diary-mobile"
    if profile_id:
        h["Profile-Id"] = str(profile_id)
    if role:
        h["X-Mes-Role"] = role
    return h


async def get(path, params=None, *, mobile=False, profile_id=None, role=None, host=None, ttl=CACHE_TTL):
    """GET к API МЭШ с одним автоматическим обновлением токена. Возвращает разобранный JSON."""
    if not logged_in():
        raise MesAuthError("вход в МЭШ не выполнен: выполните deploy/mes_login.sh на сервере")
    url = (host or BASE) + path if not path.startswith("http") else path
    key = (url, json.dumps(params or {}, sort_keys=True), profile_id, mobile)
    hit = _cache.get(key)
    if ttl and hit and time.time() - hit[0] < ttl:
        return hit[1]
    for attempt in range(2):
        access = state["access"]
        async with _http() as h:
            r = await h.get(url, params=params, headers=_headers(access, profile_id=profile_id, mobile=mobile, role=role))
        if r.status_code == 401 and attempt == 0:
            await _refresh(access)
            continue
        break
    if r.status_code == 401:
        raise MesAuthError("МЭШ не принимает токен: войдите заново (deploy/mes_login.sh)")
    if r.status_code == 403:
        raise MesError("МЭШ отказал в доступе (403): раздел недоступен для этого аккаунта или запрос не из России")
    if r.status_code >= 400:
        raise MesError(f"МЭШ ответил {r.status_code}: {clean(r.text, 200)}")
    try:
        data = r.json()
    except ValueError:
        raise MesError("МЭШ вернул не JSON")
    if ttl:
        _cache[key] = (time.time(), data)
        if len(_cache) > 300:
            _cache.clear()
    return data


def _file_host_ok(url):
    from urllib.parse import urlparse
    u = urlparse(url)
    return u.scheme == "https" and bool(u.hostname) and (u.hostname in HOSTS or u.hostname.endswith(".mos.ru"))


async def download(url):
    """Скачать файл МЭШ (только https и *.mos.ru). Возвращает (байты, тип). Вход передаётся только на school/dnevnik/uchebnik.mos.ru."""
    from urllib.parse import urljoin, urlparse
    if not logged_in():
        raise MesAuthError("вход в МЭШ не выполнен: выполните deploy/mes_login.sh на сервере")
    for attempt in range(2):
        access = state["access"]
        cur = url
        expired = False
        async with _http() as h:
            for _ in range(4):
                if not _file_host_ok(cur):
                    raise MesError("адрес файла вне mos.ru: не скачиваю")
                auth = urlparse(cur).hostname in HOSTS
                async with h.stream("GET", cur, headers=_headers(access) if auth else {}) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        cur = urljoin(cur, r.headers["location"])
                        continue
                    if r.status_code == 401 and attempt == 0 and auth:
                        expired = True
                        break
                    if r.status_code >= 400:
                        raise MesError(f"файл недоступен: МЭШ ответил {r.status_code}")
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > FILE_LIMIT:
                            raise MesError(f"файл больше {FILE_LIMIT // 1048576} МБ: не скачиваю")
                    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                    return bytes(buf), ctype
            else:
                raise MesError("слишком много перенаправлений")
        if expired:
            await _refresh(access)
    raise MesAuthError("МЭШ не принимает токен: войдите заново (deploy/mes_login.sh)")


def _find(obj, *keys):
    """Первое значение по любому из ключей в произвольно вложенном JSON."""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = _find(v, *keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, *keys)
            if r is not None:
                return r
    return None


async def me(refresh=False):
    """Кто вошёл и чей дневник читаем: ученик (id, person_id), профиль, школа, класс, договор питания."""
    if _profile and not refresh and time.time() - _profile["at"] < 3600:
        return _profile
    fam = await get("family/web/v1/profile", ttl=0)
    try:
        info = await get("https://school.mos.ru/v1/oauth/userinfo", ttl=0)
    except MesError:
        info = {}
    prof = fam.get("profile") or {}
    kids = fam.get("children") or []
    child = kids[0] if kids else prof
    school = child.get("school") or {}
    _profile.clear()
    _profile.update(
        at=time.time(), raw=fam, role=str(prof.get("type") or prof.get("role") or "").lower(), profile_id=prof.get("id"),
        student_id=child.get("id"), person_id=info.get("person_id") or child.get("contingent_guid"),
        name=" ".join(x for x in (child.get("last_name"), child.get("first_name"), child.get("middle_name")) if x),
        class_name=child.get("class_name") or "", class_unit_id=child.get("class_unit_id"),
        school=school.get("short_name") or school.get("name") or "", contract_id=_find(child, "contract_id", "contractId"),
        children=[dict(id=k.get("id"), name=" ".join(x for x in (k.get("last_name"), k.get("first_name")) if x),
                       cls=k.get("class_name")) for k in kids])
    return _profile


# ---------- оформление ----------

def _day(s, default=None):
    d = parse_when(s, TZ) if s else None
    if d is None:
        return default or datetime.now(TZ).date()
    return d.date()


def _mark(m):
    v = m.get("value")
    w = m.get("weight")
    s = f"{v}" + (f" (вес {w})" if w and w != 1 else "")
    return s + (f" — {clean(m.get('control_form_name'), 60)}" if m.get("control_form_name") else "") + \
        (f" · «{clean(m.get('comment'), 120)}»" if m.get("comment") else "")


def file_links(h):
    """Файлы, прикреплённые к ДЗ: [(название, адрес)] из материалов МЭШ и вложений."""
    out = []
    for m in (h.get("materials") or []) + (h.get("attachments") or []):
        if not isinstance(m, dict):
            continue
        for u in m.get("urls") or []:
            if isinstance(u, dict) and str(u.get("url", "")).startswith("https://") and u.get("type") in (None, "file_link", "file"):
                out.append((clean(m.get("title") or m.get("file_name") or "файл", 80), u["url"]))
        if m.get("url") and str(m["url"]).startswith("https://"):
            out.append((clean(m.get("title") or m.get("file_name") or "файл", 80), m["url"]))
    seen, res = set(), []
    for t, u in out:
        if u not in seen:
            seen.add(u)
            res.append((t, u))
    return res


def fmt_homework(items, only_undone=False):
    out = []
    for h in sorted(items, key=lambda x: (x.get("date") or "", x.get("subject_name") or "")):
        if only_undone and h.get("is_done"):
            continue
        text = clean(h.get("description") or h.get("homework") or "", 1500)
        mats = h.get("materials") or []
        extra = []
        if mats:
            extra.append(f"материалов МЭШ: {len(mats)}")
        links = file_links(h)
        if h.get("attachments") or links:
            extra.append(f"файлов: {len(h.get('attachments') or []) or len(links)}" + (" (mes_homework_files / mes_file)" if links else ""))
        if h.get("written_answer"):
            extra.append("ответ записан")
        out.append(f"[{'x' if h.get('is_done') else ' '}] {h.get('date', '?')} · {clean(h.get('subject_name'), 60)}"
                   f" (задано {h.get('date_assigned_on', '?')}, id {h.get('homework_entry_id') or h.get('homework_id')}): "
                   f"{text or '(без текста)'}" + (f" [{'; '.join(extra)}]" if extra else ""))
    return "\n".join(out)


def fmt_marks(items):
    out = []
    for m in sorted(items, key=lambda x: (x.get("date") or "", x.get("subject_name") or "")):
        out.append(f"{m.get('date', '?')} · {clean(m.get('subject_name'), 60)}: {_mark(m)}")
    return "\n".join(out)


def fmt_subject_marks(items):
    out = []
    for s in sorted(items, key=lambda x: x.get("subject_name") or ""):
        line = f"{clean(s.get('subject_name'), 60)}: средняя {s.get('average_by_all') or s.get('average') or '—'}"
        if s.get("year_mark"):
            line += f", годовая {s['year_mark']}"
        out.append(line)
        for p in s.get("periods") or []:
            ms = " ".join(str(m.get("value")) for m in p.get("marks") or [])
            tgt = (p.get("target") or {})
            hint = f" · до {tgt['value']}: ещё {tgt['remain']}" if tgt.get("value") and tgt.get("remain") is not None else ""
            out.append(f"   {clean(p.get('title'), 30)}: {p.get('value') or '—'} [{ms}]{hint}")
    return "\n".join(out)


def fmt_events(events):
    out = []
    for e in sorted(events, key=lambda x: x.get("start_at") or ""):
        t = (e.get("start_at") or "")[11:16]
        end = (e.get("finish_at") or "")[11:16]
        if e.get("source") not in ("PLAN", "LESSON", "EC", "AE", None) and not e.get("subject_name"):
            continue
        bits = [f"{(e.get('start_at') or '')[:10]} {t}–{end} {clean(e.get('subject_name') or e.get('title'), 60)}"]
        if e.get("room_number") or e.get("room_name"):
            bits.append(f"каб. {clean(e.get('room_number') or e.get('room_name'), 20)}")
        if e.get("cancelled") or e.get("is_cancelled"):
            bits.append("ОТМЕНЕНО")
        if e.get("replaced") or e.get("is_replaced"):
            bits.append("замена")
        hw = (e.get("homework") or {})
        if hw.get("descriptions"):
            bits.append("ДЗ: " + clean("; ".join(hw["descriptions"]), 300))
        marks = [mk.get("value") for mk in e.get("marks") or []]
        if marks:
            bits.append("оценка: " + ", ".join(map(str, marks)))
        if e.get("id"):
            bits.append(f"урок {e['id']} ({e.get('source') or 'PLAN'})")
        out.append(" · ".join(bits))
    return "\n".join(out)


# ---------- инструменты ----------

def rub(kop):
    v = (kop or 0) / 100
    return (f"{v:.2f}".rstrip("0").rstrip(".") or "0") + " ₽"

async def food(path, p, **params):
    """Питание: отдельный API food/meals/v3, клиент — personId ученика (договор не нужен)."""
    cid = json.dumps({"personId": p["person_id"]}, separators=(",", ":"))
    if path.endswith("balance"):
        return await get(path, {"clientIds": "[" + cid + "]"}, ttl=0)
    return await get(path, {"clientId": cid, **params}, ttl=0)


VISION_PROMPT = ("Это фото домашнего задания по школьному предмету (из электронного дневника). Перепиши всё задание дословно: "
                 "номера задач, условия, числа, формулы и выражения обычным текстом (степени как x^2, дроби как (a)/(b), корни как "
                 "sqrt(...), индексы как a_1). Рисунки и чертежи кратко опиши словами. Ничего не решай и не добавляй от себя. "
                 "Неразборчивое помечай [неразборчиво]. Если на фото несколько заданий или страниц, перепиши все по порядку.")


async def read_photo(data, ctype):
    """Распознать фото задания моделью Gemini (ключ GEMINI_API_KEY на сервере, через тот же прокси, что и у агента)."""
    import base64
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise MesError("Чтобы читать фото с ДЗ, на сервере нужен GEMINI_API_KEY (бесплатный, aistudio.google.com).")
    proxy = (os.environ.get("AGENT_PROXY") or os.environ.get("TG_PROXY") or "").strip().replace("socks5h://", "socks5://") or None
    body = {"contents": [{"role": "user", "parts": [{"text": VISION_PROMPT},
                                                    {"inlineData": {"mimeType": ctype, "data": base64.b64encode(data).decode()}}]}],
            "generationConfig": {"temperature": 0.1}}
    last = ""
    async with httpx.AsyncClient(proxy=proxy, timeout=90) as h:
        for model in (os.environ.get("MES_VISION_MODEL", "gemini-3.6-flash"), "gemini-3.5-flash-lite", "gemini-3.6-flash"):
            r = await h.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", json=body,
                             headers={"x-goog-api-key": key, "content-type": "application/json"})
            if r.status_code == 200:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts).strip()
                    if text:
                        return text
                except (KeyError, IndexError, ValueError):
                    pass
                last = "пустой ответ модели"
            else:
                last = f"{r.status_code}"
                if r.status_code in (401, 403):
                    break
    raise MesError(f"Не удалось распознать фото ({last}).")


def register(tool, Image=None):
    async def student():
        p = await me()
        if not p.get("student_id"):
            raise MesError("не нашёл ученика в профиле МЭШ (mes_profile покажет, что вернул МЭШ)")
        return p

    @tool()
    async def mes_status() -> str:
        """МЭШ: выполнен ли вход, чей дневник, когда истекает доступ."""
        if not logged_in():
            return "Вход в МЭШ не выполнен. Владелец: sudo bash deploy/mes_login.sh на сервере (QR или вставка токенов)."
        exp = jwt_expiry(state.get("access", ""))
        left = f"токен доступа действует ещё {max(0, int((exp - time.time()) // 60))} мин (обновляется сам)" if exp else "токен доступа сохранён"
        try:
            p = await me()
        except MesAuthError as e:
            return f"Токены есть, но МЭШ их не принимает: {e}"
        return f"Вход выполнен: {p['name'] or '?'}, {p['class_name'] or '?'}, {p['school'] or '?'}. {left}."

    @tool()
    async def mes_profile() -> str:
        """МЭШ: профиль и дети (для родителя), школа, класс, номер договора питания."""
        p = await me(refresh=True)
        kids = "; ".join(f"{k['name']} ({k['cls']}, id {k['id']})" for k in p["children"]) or "—"
        return (f"Ученик: {p['name']}, {p['class_name']}\nШкола: {p['school']}\nid ученика: {p['student_id']}, id профиля: {p['profile_id']}, "
                f"роль: {p['role'] or '?'}\nДоговор питания: {p['contract_id'] or 'не найден'}\nДети в профиле: {kids}")

    @tool()
    async def mes_schedule(date_from: str = "", date_to: str = "") -> str:
        """МЭШ: расписание уроков с кабинетами, заменами, отменами, оценками и ДЗ на период (по умолчанию сегодня).

        Даты: 2025-11-03, 03.11.2025, «завтра», «вчера», 7d."""
        p = await student()
        a, b = _day(date_from), _day(date_to, _day(date_from))
        data = await get("eventcalendar/v1/api/events", {
            "person_ids": p["person_id"], "begin_date": a.isoformat(), "end_date": b.isoformat(),
            "expand": "marks,homework,absence_reason_id,health_status,nonattendance_reason_id"},
            profile_id=p["profile_id"], role="student")
        events = data.get("response") if isinstance(data, dict) else data
        return fmt_events(events or []) or "Занятий в этот период нет."

    @tool()
    async def mes_homework(date_from: str = "", date_to: str = "", only_undone: bool = False) -> str:
        """МЭШ: домашние задания на период (по умолчанию от сегодня на 7 дней): предмет, текст, сделано ли, материалы.

        only_undone — только несделанные. [x] — сделано, id в скобках нужен для mes_lesson."""
        p = await student()
        a = _day(date_from)
        b = _day(date_to, a + timedelta(days=7))
        data = await get("family/web/v1/homeworks", {"from": a.isoformat(), "to": b.isoformat(), "student_id": p["student_id"]},
                         profile_id=p["profile_id"])
        items = data.get("payload") if isinstance(data, dict) else data
        return fmt_homework(items or [], only_undone) or "Домашних заданий нет."

    async def hw_items(date_from, date_to):
        p = await student()
        a = _day(date_from)
        b = _day(date_to, a + timedelta(days=7))
        data = await get("family/web/v1/homeworks", {"from": a.isoformat(), "to": b.isoformat(), "student_id": p["student_id"]},
                         profile_id=p["profile_id"])
        items = (data.get("payload") if isinstance(data, dict) else data) or []
        return sorted(items, key=lambda x: (x.get("date") or "", x.get("subject_name") or ""))

    @tool()
    async def mes_homework_files(date_from: str = "", date_to: str = "") -> str:
        """МЭШ: файлы и фото, прикреплённые к домашним заданиям (по умолчанию от сегодня на 7 дней): дата, предмет, название, адрес.

        Адрес передайте в mes_file, чтобы увидеть фото; mes_homework_photos покажет сразу все."""
        out = []
        for h in await hw_items(date_from, date_to):
            for title, url in file_links(h):
                out.append(f"{h.get('date', '?')} · {clean(h.get('subject_name'), 60)} (ДЗ {h.get('homework_entry_id')}): {title} — {url}")
        return "\n".join(out) or "Файлов в домашних заданиях за этот период нет."

    @tool()
    async def mes_file(url: str):
        """МЭШ: скачать файл по адресу из mes_homework_files / mes_lesson (только https и mos.ru). Фото возвращается картинкой;
        для остальных файлов (pdf, документы) вернётся тип и размер. Не больше 10 МБ."""
        data, ctype = await download(url)
        name_ = clean(url.rsplit("/", 1)[-1], 80)
        if ctype in IMAGE_TYPES and Image is not None:
            return [f"Файл {name_}, {len(data) // 1024} КБ", Image(data=data, format=IMAGE_TYPES[ctype])]
        return f"Файл {name_}: тип {ctype or '?'}, {len(data) // 1024} КБ. Показать можно только картинки (jpeg, png, webp, gif)."

    @tool()
    async def mes_homework_read(date_from: str = "", date_to: str = "", subject: str = "", limit: int = 4) -> str:
        """МЭШ: прочитать задания с фото в домашних заданиях и вернуть их ТЕКСТОМ (по умолчанию от сегодня на 7 дней).

        Сервер сам распознаёт фото, поэтому ассистенту не нужно уметь смотреть картинки: он получает условия задач
        (номера, числа, формулы) и сразу решает. subject — часть названия предмета (например «алгебра»), limit — сколько фото (до 6).
        Вызывайте это, когда спрашивают, что задали или просят решить ДЗ, если у задания есть файл."""
        out, left = [], max(1, min(limit, 6))
        for h in await hw_items(date_from, date_to):
            if subject and subject.casefold() not in (h.get("subject_name") or "").casefold():
                continue
            head = f"{h.get('date', '?')} · {clean(h.get('subject_name'), 60)}"
            if h.get("description"):
                head += f": {clean(h['description'], 300)}"
            for title, url in file_links(h):
                if left <= 0:
                    break
                try:
                    raw, ctype = await download(url)
                    if ctype not in IMAGE_TYPES:
                        out.append(f"{head}\n  [файл {clean(title, 60)}: не фото ({ctype or '?'}), прочитать нельзя]")
                        continue
                    left -= 1
                    out.append(f"{head}\n  Фото «{clean(title, 60)}»:\n{clean(await read_photo(raw, ctype), RAW_LIMIT)}")
                except MesError as e:
                    out.append(f"{head}\n  [{clean(title, 60)}: {e}]")
        return "\n\n".join(out) or "Фото в домашних заданиях за этот период нет."

    @tool()
    async def mes_homework_photos(date_from: str = "", date_to: str = "", subject: str = "", limit: int = 5):
        """МЭШ: сразу показать фото из домашних заданий (по умолчанию от сегодня на 7 дней). subject — часть названия предмета,
        limit — сколько фото максимум (до 8). Фото в ДЗ часто и есть само задание: ассистент может прочитать его с картинки."""
        res, left, skipped = [], max(1, min(limit, 8)), 0
        for h in await hw_items(date_from, date_to):
            if subject and subject.casefold() not in (h.get("subject_name") or "").casefold():
                continue
            for title, url in file_links(h):
                if left <= 0:
                    skipped += 1
                    continue
                try:
                    raw, ctype = await download(url)
                except MesError:
                    skipped += 1
                    continue
                if ctype in IMAGE_TYPES and Image is not None:
                    res += [f"{h.get('date', '?')} · {clean(h.get('subject_name'), 60)}: {title}", Image(data=raw, format=IMAGE_TYPES[ctype])]
                    left -= 1
                else:
                    skipped += 1
        if not res:
            return "Фото в домашних заданиях за этот период нет." + (f" Пропущено файлов (не картинки или не скачались): {skipped}." if skipped else "")
        return res + ([f"Не показано или пропущено файлов: {skipped}."] if skipped else [])

    @tool()
    async def mes_marks(date_from: str = "", date_to: str = "") -> str:
        """МЭШ: оценки за период (по умолчанию за последние 14 дней): дата, предмет, оценка, вес, комментарий."""
        p = await student()
        b = _day(date_to)
        a = _day(date_from, b - timedelta(days=14))
        data = await get("family/web/v1/marks", {"from": a.isoformat(), "to": b.isoformat(), "student_id": p["student_id"]},
                         profile_id=p["profile_id"])
        items = data.get("payload") if isinstance(data, dict) else data
        return fmt_marks(items or []) or "Оценок за этот период нет."

    @tool()
    async def mes_subject_marks() -> str:
        """МЭШ: итоги по предметам: средний балл, оценки по четвертям, сколько нужно до следующей оценки."""
        p = await student()
        data = await get("family/web/v1/subject_marks", {"student_id": p["student_id"]}, profile_id=p["profile_id"])
        items = data.get("payload") if isinstance(data, dict) else data
        return fmt_subject_marks(items or []) or "Данных по предметам нет."

    @tool()
    async def mes_lesson(lesson_id: str | int, kind: str = "PLAN") -> str:
        """МЭШ: подробности урока: тема, задание, материалы (для решения ДЗ). lesson_id и kind (PLAN или EC) берите из строки «урок N (KIND)» в mes_schedule."""
        p = await student()
        data = await get(f"family/mobile/v1/lesson_schedule_items/{lesson_id}", {"student_id": p["student_id"], "type": kind},
                         mobile=True, profile_id=p["profile_id"])
        return clean(json.dumps(data, ensure_ascii=False, indent=1), RAW_LIMIT)

    async def contract(arg):
        p = await student()
        cid = arg or p.get("contract_id") or os.environ.get("MES_CONTRACT_ID", "").strip()
        if not cid:
            bal = await food("food/meals/v3/clients/balance", p)
            cid = (bal[0].get("contractId") if isinstance(bal, list) and bal else None)
        if not cid:
            raise MesError("Номер договора питания не найден: передайте его в аргумент contract_id или задайте MES_CONTRACT_ID на сервере.")
        return p, int(cid)

    ORDER_STATUS = {0: "оформлен", 1: "оформлен", 2: "отменён", 3: "выдан"}

    @tool()
    async def mes_meals(date_from: str = "", date_to: str = "") -> str:
        """МЭШ: питание: баланс карты, что ел ученик (завтраки, обеды, буфет) за период (по умолчанию 14 дней), лимит и доступные режимы."""
        p = await student()
        b = _day(date_to)
        a = _day(date_from, b - timedelta(days=14))
        bal = await food("food/meals/v3/clients/balance", p)
        orders = await food("food/meals/v3/orders", p, **{"from": a.isoformat(), "to": b.isoformat(), "limit": 50})
        out = []
        if isinstance(bal, list) and bal:
            x = bal[0]
            lim = (x.get("expenseConstraints") or {}).get("expenseDayLimit")
            out.append(f"Баланс: {rub(x.get('balance'))}" + (f" · лимит в день {rub(lim)}" if lim else ""))
        for o in (orders or {}).get("orders", []):
            when = (o.get("deliveredAt") or o.get("createdAt") or "")[:16].replace("T", " ")
            lines = []
            for it in o.get("items") or []:
                c = it.get("complex")
                if c:
                    dishes = ", ".join(d.get("name", "") for d in c.get("complexItems") or [] if d.get("name"))
                    lines.append(f"{c.get('name', 'комплекс')}: {dishes}")
                d = it.get("dish")
                if d:
                    lines.append(f"{d.get('name', 'блюдо')} × {it.get('amount', 1)}")
            pay = rub(o.get("totalPrice")) if o.get("totalPrice") else "бесплатно (льгота)"
            out.append(f"{when} · {ORDER_STATUS.get(o.get('status'), o.get('status'))} · {pay}\n  " + "\n  ".join(lines))
        if len(out) == 1:
            out.append(f"За {a.isoformat()} — {b.isoformat()} покупок нет.")
        return clean("\n".join(out), RAW_LIMIT)

    @tool()
    async def mes_menu(date: str = "") -> str:
        """МЭШ: меню школьной столовой на день (по умолчанию сегодня): комплексы, блюда, калории, цена."""
        p = await student()
        d = _day(date).isoformat()
        data = await food("food/meals/v3/menu/complexes", p, **{"from": d, "to": d, "withPreorder": "in"})
        out = []
        for day in data or []:
            for c in day.get("items") or []:
                rows = []
                for ci in c.get("complexItems") or []:
                    x = ci.get("dish") or ci
                    kc = f", {x['calories']} ккал" if x.get("calories") else ""
                    w = f", {x['weight']} г" if x.get("weight") else ""
                    rows.append(f"  - {x.get('name', '?')}{w}{kc}")
                out.append(f"{day.get('onDate', d)} · {c.get('name')} · {rub(c.get('price'))}\n" + "\n".join(rows))
        return clean("\n".join(out) or f"На {d} меню нет.", RAW_LIMIT)

    @tool()
    async def mes_visits(date_from: str = "", date_to: str = "", contract_id: int = 0) -> str:
        """МЭШ: посещаемость: приходы и уходы из школы (турникет) и питание за период (по умолчанию неделя). contract_id — как в mes_meals."""
        p, cid = await contract(contract_id)
        b = _day(date_to)
        a = _day(date_from, b - timedelta(days=7))
        data = await get("family/mobile/v1/visits", {"student_id": p["student_id"], "contract_id": cid,
                                                     "from": a.isoformat(), "to": b.isoformat()}, mobile=True, profile_id=p["profile_id"])
        return clean(json.dumps(data, ensure_ascii=False, indent=1), RAW_LIMIT)

    @tool()
    async def mes_notifications() -> str:
        """МЭШ: уведомления дневника (новые оценки, замены, сообщения)."""
        p = await student()
        data = await get("family/mobile/v1/notifications/search", {"student_id": p["student_id"]}, mobile=True, profile_id=p["profile_id"])
        return clean(json.dumps(data, ensure_ascii=False, indent=1), RAW_LIMIT)

    @tool()
    async def mes_rating() -> str:
        """МЭШ: место в рейтинге класса и по предметам."""
        p = await student()
        today = datetime.now(TZ).date()
        data = await get("ej/rating/v1/rank/rankShort", {"personId": p["person_id"], "beginDate": (today - timedelta(days=30)).isoformat(),
                                                         "endDate": today.isoformat()}, mobile=True, profile_id=p["profile_id"])
        return clean(json.dumps(data, ensure_ascii=False, indent=1), RAW_LIMIT)

    @tool()
    async def mes_raw(path: str, params: str = "") -> str:
        """МЭШ: любой GET-запрос к API (для разделов без отдельного инструмента), с вашим входом.

        path — «family/web/v1/profile» (относительно https://school.mos.ru/api/) или полный адрес school.mos.ru,
        dnevnik.mos.ru, uchebnik.mos.ru; params — строка запроса «a=1&b=2». Только чтение."""
        from urllib.parse import parse_qsl, urlparse
        if path.startswith("http"):
            host = urlparse(path).hostname or ""
            if host not in HOSTS:
                raise MesError(f"Хост {host} не разрешён (только {', '.join(sorted(HOSTS))})")
        elif ".." in path or path.startswith("/") and "//" in path:
            raise MesError("Недопустимый путь")
        try:
            profile_id = (await me()).get("profile_id")
        except (MesError, MesAuthError):
            profile_id = None  # разведка нужна как раз тогда, когда профиль не открылся
        data = await get(path.lstrip("/") if not path.startswith("http") else path, dict(parse_qsl(params)),
                         profile_id=profile_id, ttl=0)
        return clean(json.dumps(data, ensure_ascii=False, indent=1), RAW_LIMIT)
