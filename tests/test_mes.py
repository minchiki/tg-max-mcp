"""МЭШ без сети: подменённый HTTP, токены, обновление, оформление ответов."""
import asyncio
import json
import os
import sys
import tempfile
import time

import httpx
import pytest

os.environ["MES_FILE"] = os.path.join(tempfile.mkdtemp(), "mes.json")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import mes  # noqa: E402

TOOLS = {}


def tool(write=False, **_):
    def deco(fn):
        TOOLS[fn.__name__] = fn
        return fn
    return deco


mes.register(tool)

PROFILE = {"profile": {"id": 777, "type": "student"}, "children": [{
    "id": 555, "first_name": "Иван", "last_name": "Петров", "class_name": "9А", "class_unit_id": 9,
    "school": {"short_name": "Школа 1"}, "contract_id": 424242, "contingent_guid": "guid-1"}]}
USERINFO = {"person_id": "person-uid-1"}
HOMEWORK = {"payload": [
    {"date": "2026-11-03", "date_assigned_on": "2026-10-31", "subject_name": "Алгебра", "description": "№ 123, 125", "is_done": False,
     "materials": [{"uuid": "x"}], "attachments": [], "homework_entry_id": 11},
    {"date": "2026-11-04", "date_assigned_on": "2026-10-31", "subject_name": "История", "description": "параграф 12", "is_done": True,
     "materials": [], "attachments": [{"f": 1}], "homework_entry_id": 12}]}
MARKS = {"payload": [{"date": "2026-11-01", "subject_name": "Физика", "value": "5", "weight": 2, "control_form_name": "Контрольная",
                      "comment": "молодец"}]}
SUBJECTS = {"payload": [{"subject_name": "Алгебра", "average_by_all": "4.5", "periods": [
    {"title": "1 четверть", "value": "4.6", "marks": [{"value": "5"}, {"value": "4"}], "target": {"value": 5, "remain": 2}}]}]}
EVENTS = {"response": [{"source": "PLAN", "start_at": "2026-11-02T08:30:00+03:00", "finish_at": "2026-11-02T09:10:00+03:00",
                        "subject_name": "Алгебра", "room_number": "301", "homework": {"descriptions": ["№ 123"]},
                        "marks": [{"value": "5"}]}]}
FOOD_BALANCE = [{"clientId": {"personId": "person-uid-1"}, "contractId": 99, "balance": 1300,
                 "expenseConstraints": {"expenseDayLimit": 20000}}]
FOOD_ORDERS = {"hasNext": False, "orders": [{"status": 3, "deliveredAt": "2026-11-03T11:15:17+03:00", "totalPrice": 0, "items": [
    {"complex": {"name": "Завтрак 5-11", "complexItems": [{"name": "Гречка"}, {"name": "Компот"}]}}]}]}
FOOD_MENU = [{"onDate": "2026-11-05", "items": [{"id": 1, "name": "Обед", "price": 36303, "paymentTypes": [0], "complexItems": [
    {"dish": {"name": "Пельмени", "weight": "180", "calories": 335}}]}]}]


@pytest.fixture
def net(monkeypatch):
    mes.state.clear()
    mes._cache.clear()
    mes._profile.clear()
    if os.path.exists(mes.FILE):
        os.remove(mes.FILE)
    mes.set_tokens("access-1", "refresh-1")
    calls = []

    def handler(req):
        calls.append((req.method, str(req.url.copy_with(query=None)), dict(req.headers), dict(req.url.params)))
        path = req.url.path
        if path.endswith("/token/refresh"):
            return httpx.Response(200, json={"access_token": "access-2", "refresh_token": "refresh-2"})
        if req.headers.get("authorization") == "Bearer expired":
            return httpx.Response(401, json={})
        routes = {"/api/family/web/v1/profile": PROFILE, "/v1/oauth/userinfo": USERINFO, "/api/family/web/v1/homeworks": HOMEWORK,
                  "/api/family/web/v1/marks": MARKS, "/api/family/web/v1/subject_marks": SUBJECTS,
                  "/api/eventcalendar/v1/api/events": EVENTS, "/api/food/meals/v3/clients/balance": FOOD_BALANCE,
                  "/api/food/meals/v3/orders": FOOD_ORDERS, "/api/food/meals/v3/menu/complexes": FOOD_MENU}
        return httpx.Response(200, json=routes[path]) if path in routes else httpx.Response(404, json={})

    monkeypatch.setattr(mes, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"User-Agent": "t"}))
    return calls


def run(coro):
    return asyncio.run(coro)


def test_profile_discovery_and_status(net):
    out = run(TOOLS["mes_status"]())
    assert "Петров Иван" in out and "9А" in out and "Школа 1" in out
    p = run(mes.me())
    assert p["student_id"] == 555 and p["profile_id"] == 777 and p["person_id"] == "person-uid-1" and p["contract_id"] == 424242


def test_request_carries_auth_headers(net):
    run(TOOLS["mes_homework"]("2026-11-03", "2026-11-05"))
    _, url, headers, params = [c for c in net if c[1].endswith("/homeworks")][0]
    assert headers["authorization"] == "Bearer access-1" and "aupd_token=access-1" in headers["cookie"]
    assert headers["x-mes-subsystem"] == "familyweb" and headers["profile-id"] == "777"
    assert params == {"from": "2026-11-03", "to": "2026-11-05", "student_id": "555"}


def test_homework_formatting_and_filter(net):
    all_ = run(TOOLS["mes_homework"]("2026-11-03"))
    assert "[ ] 2026-11-03 · Алгебра" in all_ and "№ 123, 125" in all_ and "материалов МЭШ: 1" in all_
    assert "[x] 2026-11-04 · История" in all_ and "файлов: 1" in all_
    todo = run(TOOLS["mes_homework"]("2026-11-03", "", True))
    assert "Алгебра" in todo and "История" not in todo


def test_marks_subject_marks_and_schedule(net):
    assert "2026-11-01 · Физика: 5 (вес 2) — Контрольная · «молодец»" in run(TOOLS["mes_marks"]("2026-10-20", "2026-11-02"))
    sm = run(TOOLS["mes_subject_marks"]())
    assert "Алгебра: средняя 4.5" in sm and "1 четверть: 4.6 [5 4]" in sm and "до 5: ещё 2" in sm
    sch = run(TOOLS["mes_schedule"]("2026-11-02"))
    assert "2026-11-02 08:30–09:10 Алгебра" in sch and "каб. 301" in sch and "ДЗ: № 123" in sch and "оценка: 5" in sch
    ev = [c for c in net if c[1].endswith("/events")][0]
    assert ev[3]["person_ids"] == "person-uid-1" and ev[2]["x-mes-role"] == "student"


def test_expired_token_refreshes_once_and_persists_new_pair(net):
    mes.set_tokens("expired", "refresh-1")
    out = run(TOOLS["mes_marks"]("2026-10-20", "2026-11-02"))
    assert "Физика" in out
    assert mes.state["access"] == "access-2" and mes.state["refresh"] == "refresh-2"
    assert json.load(open(mes.FILE))["refresh"] == "refresh-2"  # на диске до следующего запроса
    assert sum(1 for c in net if c[1].endswith("/token/refresh")) == 1


def test_parallel_requests_refresh_only_once(net):
    mes.set_tokens("expired", "refresh-1")

    async def go():
        await asyncio.gather(TOOLS["mes_marks"]("2026-10-20", "2026-11-02"), TOOLS["mes_homework"]("2026-11-03"))

    run(go())
    assert sum(1 for c in net if c[1].endswith("/token/refresh")) == 1


def test_refresh_failure_asks_to_login_again(net, monkeypatch):
    mes.set_tokens("expired", "refresh-1")

    def handler(req):
        return httpx.Response(401, json={})

    monkeypatch.setattr(mes, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(mes.MesAuthError) as e:
        run(mes.get("family/web/v1/marks", ttl=0))
    assert "mes_login.sh" in str(e.value)


def _jwt(exp):
    import base64
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def test_keepalive_refreshes_before_expiry_only(net):
    now = time.time()
    mes.set_tokens(_jwt(now + 20 * 3600), "refresh-1")
    assert run(mes.ensure_fresh(now=now)) is False  # ещё долго живёт — не трогаем
    assert not any(c[1].endswith("/token/refresh") for c in net)
    mes.set_tokens(_jwt(now + 3600), "refresh-1")
    assert run(mes.ensure_fresh(now=now)) is True  # меньше 6 часов — продлили заранее
    assert mes.state["access"] == "access-2" and json.load(open(mes.FILE))["refresh"] == "refresh-2"


def test_revoked_session_explains_cause(net, monkeypatch):
    mes.set_tokens("expired", "refresh-1")

    def handler(req):
        if req.url.path.endswith("/token/refresh"):
            return httpx.Response(403, json={"msg": "token_revoked"})
        return httpx.Response(401, json={})

    monkeypatch.setattr(mes, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(mes.MesAuthError) as e:
        run(mes.get("family/web/v1/marks", ttl=0))
    assert "token_revoked" in str(e.value) and "QR" in str(e.value)


def test_not_logged_in_message(monkeypatch):
    mes.state.clear()
    monkeypatch.setattr(mes, "FILE", os.path.join(tempfile.mkdtemp(), "none.json"))
    assert "не выполнен" in run(TOOLS["mes_status"]())
    with pytest.raises(mes.MesAuthError):
        run(mes.get("family/web/v1/marks"))


def test_tokens_file_is_private_and_reloaded_when_changed(net):
    if os.name != "nt":
        assert oct(os.stat(mes.FILE).st_mode & 0o777) == "0o600"
    d = json.load(open(mes.FILE))
    d["access"] = "access-new"
    with open(mes.FILE, "w") as f:
        json.dump(d, f)
    os.utime(mes.FILE, (time.time() + 5, time.time() + 5))
    assert mes.logged_in() and mes.state["access"] == "access-new"


def test_cache_prevents_repeated_requests(net):
    run(TOOLS["mes_marks"]("2026-10-20", "2026-11-02"))
    run(TOOLS["mes_marks"]("2026-10-20", "2026-11-02"))
    assert sum(1 for c in net if c[1].endswith("/marks")) == 1


def test_raw_allows_only_mes_hosts_and_returns_json(net):
    with pytest.raises(mes.MesError):
        run(TOOLS["mes_raw"]("https://evil.example/api/x"))
    with pytest.raises(mes.MesError):
        run(TOOLS["mes_raw"]("../../etc/passwd"))
    out = run(TOOLS["mes_raw"]("family/web/v1/marks", "from=2026-10-20&to=2026-11-02"))
    assert "Физика" in out


def test_foreign_text_is_cleaned():
    bad = [{"date": "2026-11-03", "subject_name": "Алг‮ебра", "description": "делай​ это", "is_done": False}]
    out = mes.fmt_homework(bad)
    assert "‮" not in out and "​" not in out


def test_jwt_expiry_parse():
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1900000000}).encode()).decode().rstrip("=")
    assert mes.jwt_expiry(f"x.{payload}.y") == 1900000000 and mes.jwt_expiry("not-a-jwt") is None


def test_login_with_bad_tokens_keeps_previous_login(net, monkeypatch):
    import mes_login

    def handler(req):
        return httpx.Response(401, json={})

    monkeypatch.setattr(mes, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(mes.MesAuthError):
        run(mes_login.finish("bad-access", "bad-refresh"))
    assert mes.state["access"] == "access-1" and json.load(open(mes.FILE))["access"] == "access-1"


def test_login_without_previous_login_leaves_no_file(monkeypatch):
    import mes_login
    mes.state.clear()
    monkeypatch.setattr(mes, "FILE", os.path.join(tempfile.mkdtemp(), "fresh.json"))
    monkeypatch.setattr(mes, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))))
    with pytest.raises(mes.MesAuthError):
        run(mes_login.finish("bad", ""))
    assert not os.path.exists(mes.FILE) and not mes.state


def test_login_good_tokens_are_saved_and_verified(net):
    import mes_login
    p = run(mes_login.finish("access-1", "refresh-1"))
    assert p["name"] == "Петров Иван" and json.load(open(mes.FILE))["access"] == "access-1"


def test_file_links_and_download_guard():
    hw = {"materials": [{"title": "фото.jpg", "urls": [{"url": "https://school.mos.ru/ej/attachments/files/1/original/a.jpg", "type": "file_link"}]},
                        {"title": "ссылка", "urls": [{"url": "http://evil/x", "type": "file_link"}]}],
          "attachments": []}
    assert mes.file_links(hw) == [("фото.jpg", "https://school.mos.ru/ej/attachments/files/1/original/a.jpg")]
    assert not mes._file_host_ok("https://evil.example/a.jpg") and not mes._file_host_ok("http://school.mos.ru/a.jpg")
    assert mes._file_host_ok("https://school.mos.ru/ej/a.jpg") and mes._file_host_ok("https://cdn.mos.ru/a.jpg")


def test_meals_and_menu_work_for_student_without_contract(net):
    out = run(TOOLS["mes_meals"]("2026-11-01", "2026-11-04"))
    assert "Баланс: 13 ₽" in out and "лимит в день 200 ₽" in out
    assert "выдан" in out and "бесплатно" in out and "Гречка, Компот" in out
    menu = run(TOOLS["mes_menu"]("2026-11-05"))
    assert "Обед" in menu and "бесплатно по льготе (стоимость 363.03 ₽)" in menu and "Пельмени, 180 г, 335 ккал" in menu
    q = [c[3] for c in net if c[1].endswith("/menu/complexes")][0]
    assert q["withPreorder"] == "ex" and "person-uid-1" in q["clientId"]


def test_homework_read_turns_photo_into_text(net, monkeypatch):
    mat = [{"title": "фото.jpg", "urls": [{"url": "https://school.mos.ru/ej/attachments/files/1/original/a.jpg", "type": "file_link"}]}]
    monkeypatch.setitem(HOMEWORK["payload"][0], "materials", mat)
    seen = []

    async def fake_download(url):
        return b"jpegbytes", "image/jpeg"

    async def fake_read(data, ctype):
        seen.append((data, ctype))
        return "№ 123. Решите: x^2 - 4 = 0"

    monkeypatch.setattr(mes, "download", fake_download)
    monkeypatch.setattr(mes, "read_photo", fake_read)
    out = run(TOOLS["mes_homework_read"]("2026-11-03", "2026-11-05", "алгебра"))
    assert "Алгебра" in out and "№ 123. Решите: x^2 - 4 = 0" in out and "История" not in out
    assert seen == [(b"jpegbytes", "image/jpeg")]


def test_read_photo_needs_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(mes.MesError, match="GEMINI_API_KEY"):
        run(mes.read_photo(b"x", "image/jpeg"))


def test_menu_kinds_split_free_and_paid(net, monkeypatch):
    async def fake_food(path, p, **params):
        free = params["withPreorder"] == "ex"
        items = ([{"id": 1, "name": "Обед", "price": 36303, "paymentTypes": [0]}] if free else
                 [{"id": 2, "name": "Экспресс", "price": 33300, "paymentTypes": [1]}, {"id": 1, "name": "Обед", "price": 36303, "paymentTypes": [0]}])
        return [{"onDate": "2026-11-05", "items": items}]

    monkeypatch.setattr(mes, "food", fake_food)
    import datetime as dt
    d = dt.date(2026, 11, 5)
    names = lambda k: [c["name"] for c, _ in run(mes.menu_complexes({}, d, k))]
    assert names("льготное") == ["Обед"] and names("платное") == ["Экспресс"] and names("всё") == ["Обед", "Экспресс"]
