"""Ежедневная проверка живых подключений: МЭШ и Умскул отвечают, вход цел, формат ответов не поехал.
Запуск: sudo bash deploy/selfcheck.sh. Код выхода 1, если что-то не так (видно в systemctl --failed).
Если бот-уведомления настроены и Telegram достижим, тревога уйдёт ещё и туда. Токены и тексты ответов не печатаются."""
import asyncio
import sys
from datetime import datetime

import mes
import umschool


async def check_mes():
    if not mes.logged_in():
        return "МЭШ: вход не выполнен (sudo bash deploy/mes_login.sh)"
    await mes.ensure_fresh()
    p = await mes.me(refresh=True)
    if not p.get("person_id"):
        return "МЭШ: профиль пришёл без person_id, формат мог измениться"
    today = datetime.now(mes.TZ).date()
    days = await mes.menu_complexes(p, today, "всё")
    if not isinstance(days, list):
        return "МЭШ: меню столовой пришло в неожиданном виде"
    return None


async def check_ums():
    if not umschool.logged_in():
        return None  # Умскул необязателен
    await umschool.ensure_fresh()
    prof = await umschool.profile()
    if not (prof.get("user") or {}).get("id"):
        return "Умскул: профиль без id, формат мог измениться"
    subs = await umschool.get("v3/subscriptions/my/")
    if not isinstance(subs, dict) or "active" not in subs:
        return "Умскул: подписки пришли в неожиданном виде"
    return None


async def main():
    problems = []
    for name, fn in (("МЭШ", check_mes), ("Умскул", check_ums)):
        try:
            r = await fn()
        except Exception as e:  # любая причина — это тревога, а не падение проверки
            r = f"{name}: {type(e).__name__}: {str(e)[:200]}"
        print(("ПРОБЛЕМА " + r) if r else f"ok {name}")
        if r:
            problems.append(r)
    if problems:
        try:
            import mesnotify
            await asyncio.wait_for(mesnotify.send("⚠️ Проверка плагина:\n" + "\n".join(problems)), 20)
        except Exception:
            pass
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
