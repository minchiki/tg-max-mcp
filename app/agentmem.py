"""Память агента: свой журнал переписки и заметки о каждом собеседнике.

Журнал ведётся на сервере и не зависит от мессенджера: если владелец удалит сообщение в чате, агент его всё равно помнит.
У каждой записи есть автор: «them» (собеседник), «owner» (владелец написал сам) и «agent» (написал агент).
Заметки — короткий текст, который агент сам обновляет: как общаются с этим человеком, договорённости, факты.
"""
import json
import os
import sys

import dialogs

FILE = os.environ.get("AGENT_MEMORY_FILE") or os.path.join(os.path.dirname(dialogs.FILE), "agent_memory.json")
LOG_CAP = int(os.environ.get("AGENT_MEMORY_LOG", "800"))   # сколько сообщений на чат хранить
NOTES_CAP = 1500                                           # размер заметок о собеседнике
BYS = ("them", "owner", "agent")

data = {}   # "net:chat" -> dict(title, notes, log=[dict(id, ts, by, who, text)])


def load():
    global data
    try:
        with open(FILE, encoding="utf-8") as f:
            loaded = json.load(f)
        data = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        data = {}


def save():
    try:
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        with open(FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(FILE + ".tmp", FILE)
        try:
            os.chmod(FILE, 0o600)
        except OSError:
            pass
    except OSError as e:
        print(f"AGENT memory save failed: {type(e).__name__}", file=sys.stderr, flush=True)


def entry(k, title=""):
    e = data.setdefault(k, dict(title=title, notes="", log=[]))
    if title:
        e["title"] = title
    return e


def record(k, mid, ts, by, who, text, title=""):
    """Запомнить сообщение. Повтор того же id игнорируется. Возвращает True, если запись новая."""
    e = entry(k, title)
    if mid is not None and any(str(m.get("id")) == str(mid) for m in e["log"][-LOG_CAP:]):
        return False
    e["log"].append(dict(id=mid, ts=ts, by=by if by in BYS else "them", who=who, text=text))
    if len(e["log"]) > LOG_CAP:
        del e["log"][:len(e["log"]) - LOG_CAP]
    save()
    return True


def sync(k, history, title=""):
    """Дописать в журнал то, что есть в мессенджере, но ещё не записано (старая переписка, ручные сообщения владельца)."""
    e = entry(k, title)
    known = {str(m.get("id")) for m in e["log"]}
    added = False
    for m in history:
        if str(m.get("id")) in known:
            continue
        e["log"].append(dict(id=m.get("id"), ts=m["ts"], by="owner" if m.get("mine") else "them",
                             who=m.get("who", ""), text=m.get("text", "")))
        added = True
    if added:
        e["log"].sort(key=lambda m: m["ts"])
        del e["log"][:max(0, len(e["log"]) - LOG_CAP)]
        save()


def recent(k, n):
    """Последние n сообщений в виде, который понимает агент (mine — всё, что написано от лица владельца)."""
    return [dict(m, mine=m["by"] != "them") for m in entry(k)["log"][-n:]]


def owner_samples(k, n=12):
    """Что владелец писал сам (не агент): образец манеры, даже если сообщения давно вышли из окна контекста."""
    return [m["text"] for m in entry(k)["log"] if m["by"] == "owner" and m["text"]][-n:]


def notes(k):
    return data.get(k, {}).get("notes", "")


def set_notes(k, text, title=""):
    e = entry(k, title)
    text = (text or "").strip()[:NOTES_CAP]
    if text == e["notes"]:
        return False
    e["notes"] = text
    save()
    return True


def forget(k):
    if data.pop(k, None) is None:
        return False
    save()
    return True


def summary(k):
    e = data.get(k)
    if not e:
        return None
    by = {b: sum(1 for m in e["log"] if m["by"] == b) for b in BYS}
    return dict(title=e.get("title", ""), notes=e["notes"], total=len(e["log"]), **by)
