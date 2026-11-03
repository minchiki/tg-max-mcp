import asyncio
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import mes  # noqa: E402
import mesnotify  # noqa: E402

P = {"student_id": 1, "profile_id": 2, "person_id": "u"}
NOW = datetime(2026, 11, 4, 7, 30, tzinfo=mes.TZ)  # среда


def run(c):
    return asyncio.run(c)


@pytest.fixture
def n(monkeypatch, tmp_path):
    monkeypatch.setattr(mesnotify, "FILE", str(tmp_path / "n.json"))
    mesnotify.st.clear()
    sent = []
    data = {"marks": [], "hw": [], "ev": [], "auth": False}

    async def fake_send(text):
        sent.append(text)
        return True

    async def fake_me():
        if data["auth"]:
            raise mes.MesAuthError("истёк")
        return P

    async def fake_get(path, params=None, **kw):
        if data["auth"]:
            raise mes.MesAuthError("истёк")
        if path.endswith("/marks"):
            return {"payload": data["marks"]}
        if path.endswith("/homeworks"):
            return {"payload": data["hw"]}
        if "events" in path:
            return {"response": data["ev"]}
        if path.endswith("balance"):
            return [{"balance": 1300}]
        if path.endswith("complexes"):
            return [{"items": [{"name": "Обед", "price": 36303}]}]
        return {}

    monkeypatch.setattr(mesnotify, "send", fake_send)
    monkeypatch.setattr(mes, "me", fake_me)
    monkeypatch.setattr(mes, "get", fake_get)
    return sent, data


def lesson(t, name, room="1", **kw):
    return {"start_at": f"2026-11-04T{t}:00+03:00", "subject_name": name, "room_number": room, **kw}


def test_first_check_is_silent_then_new_mark_hw_and_schedule_change_are_reported_once(n):
    sent, data = n
    data["ev"] = [lesson("08:30", "Алгебра")]
    run(mesnotify.check(NOW))
    assert sent == []
    data["marks"] = [{"id": 9, "subject_name": "Физика", "value": "5", "date": "2026-11-04"}]
    data["hw"] = [{"homework_entry_id": 5, "subject_name": "История", "date": "2026-11-05", "description": "п. 12"}]
    data["ev"] = [lesson("08:30", "Алгебра", "2", replaced=True), lesson("09:20", "Химия")]
    run(mesnotify.check(NOW))
    assert len(sent) == 1
    m = sent[0]
    assert "Новая оценка: Физика: 5" in m and "Новое ДЗ: История на 2026-11-05: п. 12" in m
    assert "Расписание на 2026-11-04 изменилось" in m and "+ 09:20 Химия" in m
    run(mesnotify.check(NOW))
    assert len(sent) == 1  # то же самое повторно не присылаем


def test_state_survives_restart(n):
    sent, data = n
    data["marks"] = [{"id": 1, "subject_name": "Алгебра", "value": "4", "date": "2026-11-03"}]
    run(mesnotify.check(NOW))
    mesnotify.load()
    run(mesnotify.check(NOW))
    assert sent == []


def test_login_loss_alerts_once_and_reports_recovery(n):
    sent, data = n
    run(mesnotify.check(NOW))
    data["auth"] = True
    run(mesnotify.check(NOW))
    run(mesnotify.check(NOW))
    assert len(sent) == 1 and "вход слетел" in sent[0]
    data["auth"] = False
    run(mesnotify.check(NOW))
    assert len(sent) == 2 and "восстановлен" in sent[1]


def test_morning_digest_once_per_day_and_only_in_time(n):
    sent, data = n
    data["ev"] = [lesson("08:30", "Алгебра", "301")]
    data["hw"] = [{"homework_entry_id": 5, "subject_name": "История", "date": "2026-11-05", "description": "п. 12", "is_done": False}]
    run(mesnotify.digest(datetime(2026, 11, 4, 15, 0, tzinfo=mes.TZ)))
    assert sent == []
    run(mesnotify.digest(NOW))
    run(mesnotify.digest(NOW))
    assert len(sent) == 1
    t = sent[0]
    assert "08:30 Алгебра, каб. 301" in t and "История" in t and "13 ₽" in t and "Обед 363.03 ₽" in t


def test_sunday_digest_has_subject_summary(n, monkeypatch):
    sent, data = n
    run(mesnotify.digest(datetime(2026, 11, 8, 18, 5, tzinfo=mes.TZ)))
    assert len(sent) == 1 and "Итоги недели" in sent[0]


def test_enabled_needs_token_and_numeric_chat(monkeypatch):
    monkeypatch.delenv("MES_BOT_TOKEN", raising=False)
    assert not mesnotify.enabled()
    monkeypatch.setenv("MES_BOT_TOKEN", "t")
    monkeypatch.setenv("MES_BOT_CHAT", "abc")
    assert not mesnotify.enabled()
    monkeypatch.setenv("MES_BOT_CHAT", "8311057308")
    assert mesnotify.enabled()
