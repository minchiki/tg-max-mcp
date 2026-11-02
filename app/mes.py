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
        if h.get("attachments"):
            extra.append(f"файлов: {len(h['attachments'])}")
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

def register(tool):
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

    @tool()
    async def mes_meals(first: int = 14) -> str:
        """МЭШ: питание: баланс и покупки по дням (договор берётся из профиля)."""
        p = await student()
        if not p.get("contract_id"):
            return "Договор питания в профиле не найден: у ученического аккаунта его обычно нет, он привязан к аккаунту родителя. Питание и посещаемость доступны при входе через родителя."
        data = await get("family/mobile/v1/day_balance_info", {"contract_id": p["contract_id"], "first": max(1, min(first, 60))},
                         mobile=True, profile_id=p["profile_id"])
        return clean(json.dumps(data, ensure_ascii=False, indent=1), RAW_LIMIT)

    @tool()
    async def mes_visits(date_from: str = "", date_to: str = "") -> str:
        """МЭШ: посещаемость: приходы и уходы из школы (турникет) и питание за период (по умолчанию неделя)."""
        p = await student()
        if not p.get("contract_id"):
            return "Договор питания в профиле не найден: у ученического аккаунта его обычно нет, он привязан к аккаунту родителя. Питание и посещаемость доступны при входе через родителя."
        b = _day(date_to)
        a = _day(date_from, b - timedelta(days=7))
        data = await get("family/mobile/v1/visits", {"student_id": p["student_id"], "contract_id": p.get("contract_id") or "",
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
