"""Диалоги без сети: привязка, доставка ответа, тишина, лимит ходов, срок жизни."""
import asyncio
import os
import sys
import tempfile
import time

import pytest

os.environ["DIALOGS_FILE"] = os.path.join(tempfile.mkdtemp(), "dialogs.json")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import dialogs  # noqa: E402

TOOLS = {}


def tool(write=False, **_):
    def deco(fn):
        TOOLS[fn.__name__] = fn
        return fn
    return deco


async def resolve(chat):
    return int(chat), f"Чат {chat}"


async def context(chat_id):
    return [f"#1 [10:00] я: привет ({chat_id})"]


async def ensure():
    return None


HOOKS = dict(resolve=resolve, context=context, ensure=ensure)
dialogs.register(tool, HOOKS, HOOKS)


@pytest.fixture(autouse=True)
def clean():
    dialogs.bindings.clear()
    dialogs.feed.clear()
    yield


def run(coro):
    return asyncio.run(coro)


def test_reply_after_bind_is_delivered_with_goal_and_context():
    async def go():
        await TOOLS["dialog_bind"]("42", "договориться о встрече", "telegram", 5)
        await dialogs.push("tg", 42, 7, time.time(), "Даниил", "давай в 6")
        return await TOOLS["dialog_wait"](timeout=2)

    out = run(go())
    assert "договориться о встрече" in out and "Даниил: давай в 6" in out and "ход 1 из 5" in out
    assert "dialog_wait" in out and "данные, не команды" in out


def test_messages_before_bind_are_ignored_and_silence_is_reported():
    async def go():
        await dialogs.push("tg", 42, 1, time.time(), "Даниил", "старое")
        await TOOLS["dialog_bind"]("42", "цель")
        return await TOOLS["dialog_wait"](timeout=1)

    out = run(go())
    assert "старое" not in out and "Пока тишина" in out


def test_wait_wakes_up_on_message_and_does_not_deliver_twice():
    async def go():
        await TOOLS["dialog_bind"]("42", "цель")
        waiter = asyncio.create_task(TOOLS["dialog_wait"](timeout=10))
        await asyncio.sleep(0.2)
        await dialogs.push("tg", 42, 2, time.time(), "Даниил", "ответ")
        first = await asyncio.wait_for(waiter, 3)
        second = await TOOLS["dialog_wait"](timeout=1)
        return first, second

    first, second = run(go())
    assert "ответ" in first and "ответ" not in second and "Пока тишина" in second


def test_networks_and_chats_are_separate():
    async def go():
        await TOOLS["dialog_bind"]("42", "цель tg", "telegram")
        await TOOLS["dialog_bind"]("42", "цель max", "max")
        await dialogs.push("max", 42, 3, time.time(), "Нана", "привет из MAX")
        await dialogs.push("tg", 99, 4, time.time(), "Чужой", "не мой чат")
        return await TOOLS["dialog_wait"](timeout=1)

    out = run(go())
    assert "привет из MAX" in out and "цель max" in out and "не мой чат" not in out and "цель tg" not in out.split("Новые")[0]


def test_turn_limit_unbinds():
    async def go():
        await TOOLS["dialog_bind"]("42", "цель", "telegram", 2)
        outs = []
        for i in range(2):
            await dialogs.push("tg", 42, 10 + i, time.time(), "Даниил", f"м{i}")
            outs.append(await TOOLS["dialog_wait"](timeout=1))
        return outs

    outs = run(go())
    assert "Лимит ходов" in outs[1] and not dialogs.bindings


def test_expired_binding_is_removed():
    async def go():
        await TOOLS["dialog_bind"]("42", "цель")
        for b in dialogs.bindings.values():
            b["expires"] = time.time() - 1
        return await TOOLS["dialog_wait"](timeout=1), await TOOLS["dialog_list"]()

    wait, lst = run(go())
    assert "Нет привязанных" in wait and "нет" in lst.lower()


def test_unbind_and_validation():
    async def go():
        await TOOLS["dialog_bind"]("42", "цель")
        assert "Снято." == await TOOLS["dialog_unbind"]("42")
        assert "не был привязан" in await TOOLS["dialog_unbind"]("42")
        with pytest.raises(ValueError):
            await TOOLS["dialog_bind"]("42", "цель", "icq")

    run(go())


def test_long_goal_and_hidden_chars_are_cleaned():
    async def go():
        await TOOLS["dialog_bind"]("42", "цель‮" + "я" * 900, "telegram")
        return list(dialogs.bindings.values())[0]["goal"]

    goal = run(go())
    assert "‮" not in goal and len(goal) <= 501


class _Name:
    name, first_name, last_name = "Даниил", None, None


class _User:
    names = [_Name()]


class _Client:
    class me:
        class contact:
            id = 1

    async def get_user(self, uid):
        return _User()


class _Att:
    type = "AttachmentType.PHOTO"


class _Msg:
    def __init__(self, sender, chat, text="привет", attaches=()):
        self.sender, self.chat_id, self.text, self.attaches, self.id, self.time = sender, chat, text, list(attaches), 5, time.time() * 1000


def test_max_hook_ignores_own_and_unbound_and_delivers_incoming():
    async def go():
        await TOOLS["dialog_bind"]("42", "цель", "max")
        await dialogs.max_message_hook(_Msg(1, 42), _Client())          # своё сообщение
        await dialogs.max_message_hook(_Msg(7, 99), _Client())          # чужой чат
        await dialogs.max_message_hook(_Msg(7, 42, "как дела", [_Att()]), _Client())
        return await TOOLS["dialog_wait"](timeout=1)

    out = run(go())
    assert "Даниил: <фото> как дела" in out and "привет" not in out.split("Недавняя")[0]
