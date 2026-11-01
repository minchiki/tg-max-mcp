"""Автономный агент без сети: модель подменена, мессенджеры — заглушки."""
import asyncio
import os
import sys
import tempfile
import time

import pytest

os.environ.setdefault("DIALOGS_FILE", os.path.join(tempfile.mkdtemp(), "dialogs.json"))
os.environ["AGENT_DEBOUNCE"] = "0.05"
os.environ["AGENT_HUMAN_DELAY"] = "0"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import agent  # noqa: E402
import dialogs  # noqa: E402

TOOLS = {}


def tool(write=False, **_):
    def deco(fn):
        TOOLS[fn.__name__] = fn
        return fn
    return deco


class World:
    """Заглушка мессенджеров: что отправлено, удалено и о чём уведомили владельца."""

    def __init__(self):
        self.sent, self.deleted, self.notes, self.next_id = [], [], [], 1000
        self.decisions = []   # очередь решений «модели»
        self.calls = []       # что модель получила
        self.gate = None      # asyncio.Event: подержать модель, чтобы проверить гонку со «стоп»

    async def resolve(self, chat):
        return int(chat), f"Чат {chat}"

    async def find(self, q):
        return [(7, "Даниил")] if "дани" in q.casefold() else [(8, "Данил"), (9, "Данила")] if q.casefold() == "дан" else []

    async def history(self, chat, n):
        return [dict(id=1, mine=False, who="Даниил", text="привет", ts=time.time())]

    async def send(self, chat, text):
        self.next_id += 1
        self.sent.append((chat, text, self.next_id))
        return self.next_id

    async def delete(self, chat, mid):
        self.deleted.append((chat, mid))

    async def typing(self, chat, seconds):
        return None

    async def ensure(self):
        return None

    async def notify(self, text):
        self.notes.append(text)

    async def decide(self, t, history, new, kickoff):
        self.calls.append((t["task"], [e["text"] for e in new], kickoff))
        if self.gate is not None:
            await self.gate.wait()
        return self.decisions.pop(0) if self.decisions else dict(action="wait", text="", reason="")


@pytest.fixture
def w(monkeypatch):
    world = World()
    h = dict(resolve=world.resolve, find=world.find, history=world.history, send=world.send, delete=world.delete,
             typing=world.typing, ensure=world.ensure)
    TOOLS.clear()
    agent.tasks.clear()
    agent._rt.clear()
    agent._sent.clear()
    dialogs.bindings.clear()
    dialogs.busy_checks.clear()
    dialogs.net_sources.clear()
    monkeypatch.setattr(agent, "decide", world.decide)
    agent.register(tool, h, h, world.notify)
    dialogs.register(tool, dict(resolve=world.resolve, context=lambda c: [], ensure=world.ensure))
    dialogs.busy_checks.append(agent.active)
    yield world
    for rt in agent._rt.values():
        rt["runner"].cancel()


def run(coro):
    return asyncio.run(coro)


async def settle(t=0.3):
    await asyncio.sleep(t)


def reply(text):
    return dict(action="reply", text=text, reason="")


def test_start_kickoff_writes_first_then_replies_to_incoming(w):
    async def go():
        w.decisions += [reply("Привет! Давай завтра в 6?"), reply("Отлично, до встречи")]
        msg = await TOOLS["agent_start"]("7", "договориться о встрече", "telegram", "коротко")
        await settle()
        await agent.on_incoming("tg", 7, 11, time.time(), "Даниил", "ок, в 6 норм")
        await settle(0.5)
        return msg

    msg = run(go())
    assert "Агент запущен" in msg
    assert [s[1] for s in w.sent] == ["Привет! Давай завтра в 6?", "Отлично, до встречи"]
    assert w.calls[0][2] is True and w.calls[1][1] == ["ок, в 6 норм"]


def test_wait_does_not_send_and_burst_is_one_decision(w):
    async def go():
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        w.decisions += [dict(action="wait", text="", reason="ждём")]
        for i in range(3):
            await agent.on_incoming("tg", 7, 20 + i, time.time(), "Даниил", f"м{i}")
        await settle(0.5)

    run(go())
    assert w.sent == [] and len(w.calls) == 2 and w.calls[1][1] == ["м0", "м1", "м2"]


def test_done_sends_goodbye_stops_and_notifies(w):
    async def go():
        w.decisions += [dict(action="wait", text="", reason=""), dict(action="done", text="Спасибо, договорились!", reason="встреча в 18:00")]
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        await agent.on_incoming("tg", 7, 11, time.time(), "Даниил", "давай в 18:00")
        await settle(0.5)

    run(go())
    assert [s[1] for s in w.sent] == ["Спасибо, договорились!"]
    assert not agent.tasks and any("цель достигнута" in n and "встреча в 18:00" in n for n in w.notes)


def test_escalate_stops_without_sending_and_quotes_last_message(w):
    async def go():
        w.decisions += [dict(action="wait", text="", reason=""), dict(action="escalate", text="", reason="просит перевести деньги")]
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        await agent.on_incoming("tg", 7, 11, time.time(), "Даниил", "скинь 5000 на карту")
        await settle(0.5)

    run(go())
    assert w.sent == [] and not agent.tasks
    assert any("нужен ты" in n and "просит перевести деньги" in n and "скинь 5000" in n for n in w.notes)


def test_reply_limit_stops_agent(w):
    async def go():
        w.decisions += [reply("раз"), reply("два")]
        await TOOLS["agent_start"]("7", "задача", "telegram", "", 2)
        await settle()
        await agent.on_incoming("tg", 7, 11, time.time(), "Даниил", "а?")
        await settle(0.5)
        await agent.on_incoming("tg", 7, 12, time.time(), "Даниил", "ещё?")
        await settle(0.4)

    run(go())
    assert len(w.sent) == 2 and not agent.tasks and any("лимит ответов" in n for n in w.notes)


def test_duplicate_reply_is_suppressed(w):
    async def go():
        w.decisions += [reply("привет"), reply("привет")]
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        await agent.on_incoming("tg", 7, 11, time.time(), "Даниил", "?")
        await settle(0.5)

    run(go())
    assert [s[1] for s in w.sent] == ["привет"]


def test_stop_word_in_chat_stops_and_deletes_it(w):
    async def go():
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        await agent.on_outgoing("tg", 7, 555, "Стоп!")
        return list(agent.tasks)

    assert run(go()) == [] and w.deleted == [(7, 555)] and any("остановлен" in n for n in w.notes)


def test_ordinary_owner_message_does_not_stop_and_stop_inside_sentence_neither(w):
    async def go():
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        await agent.on_outgoing("tg", 7, 556, "я сам напишу позже")
        await agent.on_outgoing("tg", 7, 557, "стоп, давай по-другому")
        return list(agent.tasks)

    assert run(go()) == ["tg:7"] and not w.deleted


def test_agent_own_messages_are_not_mistaken_for_owner_commands(w):
    async def go():
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        agent._sent.append(("tg", 7, 600))
        await agent.on_outgoing("tg", 7, 600, "стоп")
        return list(agent.tasks)

    assert run(go()) == ["tg:7"]


def test_stop_during_thinking_prevents_sending(w):
    async def go():
        w.gate = asyncio.Event()
        w.decisions += [reply("не должно уйти")]
        await TOOLS["agent_start"]("7", "задача")
        await settle(0.1)                       # модель «думает» над первым ходом
        await agent.on_outgoing("tg", 7, 700, "стоп")
        w.gate.set()
        await settle(0.3)

    run(go())
    assert w.sent == []


def test_saved_messages_commands_start_status_stop(w):
    async def go():
        await agent.on_outgoing("tg", 1, 1, "агент Даниил: договорись о встрече завтра, коротко и на ты", saved=True)
        await settle()
        started = dict(agent.tasks)
        await agent.on_outgoing("tg", 1, 2, "агент статус", saved=True)
        await agent.on_outgoing("tg", 1, 3, "стоп", saved=True)
        return started

    started = run(go())
    t = started["tg:7"]
    assert t["task"].startswith("договорись о встрече завтра") and not agent.tasks
    assert any("Агент запущен" in n for n in w.notes) and any("договорись о встрече" in n and "ответов 0" in n for n in w.notes)
    assert any("остановлен" in n for n in w.notes)


def test_saved_commands_ambiguous_unknown_and_stop_by_name(w):
    async def go():
        await agent.on_outgoing("tg", 1, 1, "агент Дан: привет", saved=True)
        await agent.on_outgoing("tg", 1, 2, "агент Неизвестный: привет", saved=True)
        await agent.on_outgoing("tg", 1, 3, "агент Даниил: задача", saved=True)
        await settle()
        await agent.on_outgoing("tg", 1, 4, "стоп Петя", saved=True)
        still = list(agent.tasks)
        await agent.on_outgoing("tg", 1, 5, "стоп даниил", saved=True)
        return still, list(agent.tasks)

    still, after = run(go())
    assert still == ["tg:7"] and after == []
    assert any("несколько чатов" in n for n in w.notes) and any("Не нашёл чат" in n for n in w.notes)


def test_agent_notifications_in_saved_are_ignored(w):
    async def go():
        await agent.on_outgoing("tg", 1, 1, "🤖 Агент «Даниил» остановлен: агент Даниил: x", saved=True)
        return list(agent.tasks)

    assert run(go()) == []


def test_without_api_key_agent_does_not_start(w, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    async def go():
        return await TOOLS["agent_start"]("7", "задача")

    assert "ANTHROPIC_API_KEY" in run(go()) and not agent.tasks


def test_chat_cannot_have_agent_and_dialog_at_once(w):
    async def go():
        await TOOLS["dialog_bind"]("7", "цель")
        a = await TOOLS["agent_start"]("7", "задача")
        dialogs.bindings.clear()
        await TOOLS["agent_start"]("7", "задача")
        b = await TOOLS["dialog_bind"]("7", "цель")
        c = await TOOLS["agent_start"]("7", "ещё")
        return a, b, c

    a, b, c = run(go())
    assert "dialog_unbind" in a and "автономный агент" in b and "уже работает" in c


def test_send_failure_stops_agent_and_tells_owner(w, monkeypatch):
    async def deny(chat, text):
        raise PermissionError("чат не в белом списке")

    w.decisions += [reply("привет")]

    async def go():
        agent.hooks["tg"]["send"] = deny
        await TOOLS["agent_start"]("7", "задача")
        await settle()

    run(go())
    assert not agent.tasks and any("не удалось отправить" in n and "белом списке" in n for n in w.notes)


def test_repeated_model_errors_stop_agent(w, monkeypatch):
    async def boom(t, history, new, kickoff):
        raise RuntimeError("упала")

    monkeypatch.setattr(agent, "decide", boom)

    async def go():
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        for i in range(3):
            await agent.on_incoming("tg", 7, 30 + i, time.time(), "Даниил", "?")
            await settle(0.3)

    run(go())
    assert not agent.tasks and any("модель недоступна" in n for n in w.notes)


def test_expired_agent_stops(w):
    async def go():
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        agent.tasks["tg:7"]["expires"] = time.time() - 1
        await agent.on_incoming("tg", 7, 40, time.time(), "Даниил", "?")
        await settle(0.5)

    run(go())
    assert not agent.tasks and any("истёк срок" in n for n in w.notes)


def test_task_and_style_are_cleaned(w):
    async def go():
        await TOOLS["agent_start"]("7", "задача‮" + "я" * 2000, "telegram", "стиль​")
        return dict(agent.tasks["tg:7"])

    t = run(go())
    assert "‮" not in t["task"] and len(t["task"]) <= 801 and "​" not in t["style"]


def test_stop_word_normalisation():
    assert agent._norm("  Стоп!!! ") in agent.STOP_WORDS and agent._norm("/STOP") in agent.STOP_WORDS
    assert agent._norm("стоп, давай") not in agent.STOP_WORDS


def test_messages_older_than_the_task_are_ignored(w):
    async def go():
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        await agent.on_incoming("tg", 7, 50, time.time() - 600, "Даниил", "старое сообщение")
        await settle(0.4)

    run(go())
    assert len(w.calls) == 1  # только стартовый ход


# ---------- Gemini ----------

import httpx  # noqa: E402


def _gemini_world(monkeypatch, handler):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key")
    monkeypatch.delenv("AGENT_BRAIN", raising=False)
    monkeypatch.setattr(agent, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(agent.asyncio, "sleep", lambda s: real_sleep(0))


def _ok(text, **extra):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP", **extra}],
                                     "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})


T = dict(task="встреча", style="", title="Даниил", net="tg", replies=0, max_replies=5)
H = [dict(id=1, mine=False, who="Даниил", text="привет", ts=time.time())]


def test_gemini_is_chosen_by_key_and_request_is_well_formed(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"], seen["key"], seen["body"] = str(req.url), req.headers.get("x-goog-api-key"), req.read().decode()
        return _ok('{"action": "reply", "text": "Привет, давай в 6?", "reason": ""}')

    _gemini_world(monkeypatch, handler)
    assert agent.brain() == "gemini"
    d = run(agent.decide(T, H, [dict(who="Даниил", text="привет", id=1, ts=0)], False))
    assert d == dict(action="reply", text="Привет, давай в 6?", reason="")
    assert "gemini-3.6-flash:generateContent" in seen["url"] and "AIza" not in seen["url"] and seen["key"] == "AIza-test-key"
    assert "responseMimeType" in seen["body"] and "<chat>" in seen["body"] and "встреча" in seen["body"]


def test_gemini_accepts_fenced_json_and_skips_thought_parts(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "думаю...", "thought": True}, {"text": '```json\n{"action":"wait","text":"","reason":"ок"}\n```'}]}}]})

    _gemini_world(monkeypatch, handler)
    assert run(agent.decide(T, H, [], False))["action"] == "wait"


def test_gemini_retries_bad_json_once_then_works(monkeypatch):
    replies = iter(["вот ответ без json", '{"action":"done","text":"Спасибо","reason":"готово"}'])
    _gemini_world(monkeypatch, lambda req: _ok(next(replies)))
    assert run(agent.decide(T, H, [], False))["action"] == "done"


def test_gemini_drops_thinking_config_on_400(monkeypatch):
    bodies = []

    def handler(req):
        bodies.append(req.read().decode())
        if "thinkingConfig" in bodies[-1]:
            return httpx.Response(400, json={"error": {"message": "Unknown field thinkingConfig"}})
        return _ok('{"action":"wait","text":"","reason":""}')

    _gemini_world(monkeypatch, handler)
    assert run(agent.decide(T, H, [], False))["action"] == "wait" and len(bodies) == 2


def test_gemini_rate_limit_retries_then_raises(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})

    _gemini_world(monkeypatch, handler)
    with pytest.raises(agent.RateLimitError):
        run(agent.decide(T, H, [], False))
    assert len(calls) == 4


def test_gemini_bad_key_raises_authentication_error(monkeypatch):
    _gemini_world(monkeypatch, lambda req: httpx.Response(400, json={"error": {"message": "API key not valid. Please pass a valid API key."}}))
    with pytest.raises(agent.AuthenticationError):
        run(agent.decide(T, H, [], False))


def test_gemini_safety_block_escalates(monkeypatch):
    _gemini_world(monkeypatch, lambda req: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    d = run(agent.decide(T, H, [], False))
    assert d["action"] == "escalate" and "Gemini" in d["reason"]


def test_auth_error_from_brain_stops_agent_with_clear_message(w, monkeypatch):
    async def bad(t, history, new, kickoff):
        raise agent.AuthenticationError("ключ")

    monkeypatch.setattr(agent, "decide", bad)

    async def go():
        await TOOLS["agent_start"]("7", "задача")
        await settle()

    run(go())
    assert not agent.tasks and any("ключ не принят" in n for n in w.notes)


def test_brain_selection_and_missing_key(monkeypatch):
    for k in ("GEMINI_API_KEY", "AGENT_BRAIN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert agent.brain() == "anthropic" and agent.api_key_missing()
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert agent.brain() == "gemini" and not agent.api_key_missing() and agent.model_name() == "gemini-3.6-flash"
    monkeypatch.setenv("AGENT_BRAIN", "anthropic")
    assert agent.brain() == "anthropic" and agent.api_key_missing() and agent.model_name() == "claude-opus-5-5"


def test_gemini_start_message_warns_about_privacy(w, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")

    async def go():
        return await TOOLS["agent_start"]("7", "задача")

    assert "уходят в Google" in run(go())


def test_gemini_overloaded_main_model_falls_back_to_lite(monkeypatch):
    models = []

    def handler(req):
        model = str(req.url).split("/models/")[1].split(":")[0]
        models.append(model)
        if model == "gemini-3.6-flash":
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return _ok('{"action":"reply","text":"ок","reason":""}')

    _gemini_world(monkeypatch, handler)
    d = run(agent.decide(T, H, [], False))
    assert d["text"] == "ок" and models[0] == "gemini-3.6-flash" and models[-1] == "gemini-3.5-flash-lite"


def test_gemini_connection_drop_also_falls_back(monkeypatch):
    models = []

    def handler(req):
        model = str(req.url).split("/models/")[1].split(":")[0]
        models.append(model)
        if model == "gemini-3.6-flash":
            raise httpx.RemoteProtocolError("Server disconnected")
        return _ok('{"action":"wait","text":"","reason":""}')

    _gemini_world(monkeypatch, handler)
    assert run(agent.decide(T, H, [], False))["action"] == "wait" and models == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]


def test_escalate_may_send_one_honest_message_before_stopping(w):
    async def go():
        w.decisions += [dict(action="wait", text="", reason=""),
                        dict(action="escalate", text="Это ИИ-помощник Магомеда, он ответит сам.", reason="спросили, бот ли это")]
        await TOOLS["agent_start"]("7", "задача")
        await settle()
        await agent.on_incoming("tg", 7, 11, time.time(), "Даниил", "ты бот?")
        await settle(0.5)

    run(go())
    assert [s[1] for s in w.sent] == ["Это ИИ-помощник Магомеда, он ответит сам."] and not agent.tasks
    assert any("нужен ты" in n for n in w.notes)
