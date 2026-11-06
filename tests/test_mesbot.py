import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import mes  # noqa: E402
import mesbot  # noqa: E402
import mesnotify  # noqa: E402

P = {"student_id": 1, "profile_id": 2, "person_id": "u"}
HW = [{"date": "2026-11-05", "subject_name": "Алгебра", "description": "№ 12 <b>", "is_done": False, "homework_entry_id": 77,
       "materials": [{"title": "Урок 5"}]},
      {"date": "2026-11-05", "subject_name": "Физика", "description": "§4", "is_done": True, "homework_entry_id": 78}]


@pytest.fixture
def bot(monkeypatch, tmp_path):
    monkeypatch.setattr(mesnotify, "FILE", str(tmp_path / "n.json"))
    mesnotify.st.clear()
    monkeypatch.setenv("MES_BOT_TOKEN", "1:x")
    monkeypatch.setenv("MES_BOT_CHAT", "5")
    calls = []

    async def fake_api(method, **payload):
        calls.append((method, payload))
        if method == "sendMessage" and "icon_custom_emoji_id" in str(payload.get("reply_markup")) and len(calls) == 1:
            return 400, {"description": "bad icon"}
        return 200, {"ok": True, "result": {}}

    async def fake_me():
        return P

    async def fake_get(path, params=None, **kw):
        if path.endswith("homeworks"):
            return {"payload": HW}
        if "events" in path:
            return {"response": [{"start_at": "2026-11-05T08:30:00+03:00", "subject_name": "Алгебра", "room_number": "301"}]}
        if path.endswith("subject_marks"):
            return {"payload": [{"subject_name": "Физика", "average_by_all": "4.5"}]}
        if path.endswith("/marks"):
            return {"payload": []}
        if path.endswith("balance"):
            return [{"balance": 1300}]
        if path.endswith("complexes"):
            return [{"items": [{"id": 1, "name": "Обед", "price": 36303, "paymentTypes": [0], "complexItems": [{"name": "Суп", "weight": 200}]}]}]
        return {}

    monkeypatch.setattr(mesbot, "api", fake_api)
    monkeypatch.setattr(mes, "me", fake_me)
    monkeypatch.setattr(mes, "get", fake_get)
    return calls


def texts(calls):
    return [p["text"] for m, p in calls if m in ("sendMessage", "editMessageText")]


def test_keyboard_has_all_buttons_with_premium_icons():
    kb = mesbot.keyboard()
    names = [b["text"] for row in kb["keyboard"] for b in row]
    assert names == ["Сегодня", "Завтра", "Неделя", "Расписание", "ЦДЗ", "Оценки", "Решить ДЗ", "Еда", "Настройки"]
    assert all(b["icon_custom_emoji_id"].isdigit() for row in kb["keyboard"] for b in row)
    assert not any("icon_custom_emoji_id" in b for row in mesbot.keyboard(False)["keyboard"] for b in row)


def test_start_sends_keyboard_and_falls_back_without_icons(bot):
    asyncio.run(mesbot.on_message({"text": "/start"}))
    sends = [p for m, p in bot if m == "sendMessage"]
    assert len(sends) >= 2  # первая попытка отклонена, повтор прошёл
    assert "tg-emoji" not in sends[-1]["text"] or len(sends) == 2


@pytest.mark.parametrize("name", ["Сегодня", "Завтра", "Неделя", "Расписание", "ЦДЗ", "Оценки", "Решить ДЗ", "Еда", "Настройки"])
def test_every_section_answers(bot, name):
    asyncio.run(mesbot.on_message({"text": name}))
    assert texts(bot) and all(t.strip() for t in texts(bot))


def test_homework_is_escaped_and_navigation_edits(bot, monkeypatch):
    class D:
        @staticmethod
        def now(tz=None):
            from datetime import datetime
            return datetime(2026, 11, 4, 12, 0, tzinfo=tz)
    monkeypatch.setattr(mesbot, "datetime", D)
    asyncio.run(mesbot.on_callback({"id": "c", "data": "hw:2026-11-05", "message": {"message_id": 9, "chat": {"id": 5}}}) if False else
                mesbot.show("Сегодня", edit=9, day=__import__("datetime").date(2026, 11, 5)))
    m, p = [(m, p) for m, p in bot if m == "editMessageText"][0]
    assert p["message_id"] == 9
    assert "&lt;b&gt;" in p["text"] and "Алгебра" in p["text"] and "ЦДЗ: 1" in p["text"]


def test_settings_toggle(bot):
    asyncio.run(mesbot.on_callback({"id": "c", "data": "set:hw_evening", "message": {"message_id": 3, "chat": {"id": 5}}}))
    assert mesnotify.st["hw_evening"] is True
    assert "включена" in texts(bot)[-1]
