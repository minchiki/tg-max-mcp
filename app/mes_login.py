"""Вход в МЭШ: сохраняет токены в MES_FILE, дальше сервер обновляет их сам.

  python mes_login.py            QR-вход через login.mos.ru (отсканировать приложением mos.ru / «Госуслуги Москвы»)
  python mes_login.py --tokens   вставить aupd_token и aupd_refresh_token руками (надёжнее всего)
  python mes_login.py --stdin    то же, но две строки (access, refresh) читаются из stdin

Где взять токены: в браузере открыть school.mos.ru, войти, F12 → Application (Приложение) → Cookies → school.mos.ru,
скопировать значения aupd_token и aupd_refresh_token. Они дают доступ к дневнику: никому не показывайте.
"""
import asyncio
import getpass
import html
import os
import re
import sys
import time

import httpx

import mes

UA = mes.UA
PULL = "https://login.mos.ru/sps/login/methods/headless/qrCode/pull"
COMPLETE = "https://login.mos.ru/sps/login/methods/qrCode/complete"


def show_qr(link):
    import qrcode
    qr = qrcode.QRCode()
    qr.add_data(link)
    print("\nОтсканируйте QR приложением mos.ru («Госуслуги Москвы») на телефоне:\n")
    qr.print_ascii()
    print(f"\nили откройте ссылку на телефоне: {link}\n")


async def qr_login():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as h:
        r = await h.get("https://school.mos.ru/v3/auth/sudir/login")
        m = re.search(r'url=([^"\'>]+)', r.text)
        if not m:
            raise SystemExit("МЭШ изменил страницу входа: используйте python mes_login.py --tokens")
        await h.get(html.unescape(m.group(1)), headers={"Referer": "https://school.mos.ru/"})
        shown, deadline = None, time.time() + 180
        ajax = {"Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest"}
        while time.time() < deadline:
            d = (await h.get(PULL, headers=ajax)).json()
            cmd = d.get("command")
            if cmd == "showQRCode" and d.get("link") != shown:
                shown = d["link"]
                show_qr(shown)
            elif cmd == "askForConfirm" and shown != "confirm":
                shown = "confirm"
                print("Подтвердите вход в приложении…")
            elif cmd == "needComplete":
                break
            elif cmd == "needRefresh":
                raise SystemExit("Код устарел: запустите вход заново")
            await asyncio.sleep(1)
        else:
            raise SystemExit("Время вышло: запустите вход заново")
        origin = h.cookies.get("origin") or ""
        bo = origin.split("|", 1)[1] if "|" in origin else ""
        r = await h.post(COMPLETE, content=b"", headers={"Origin": "https://login.mos.ru",
                                                         "Referer": f"https://login.mos.ru/sps/login/methods/password?bo={bo}"})
        if "/methods2/" in str(r.url) or "/ur/askToTrust" in str(r.url):
            raise SystemExit("Нужен второй фактор (СМС / звонок) или подтверждение доверия: войдите в браузере "
                             "и вставьте токены: python mes_login.py --tokens")
        access = refresh = None
        for c in h.cookies.jar:
            if c.name == "aupd_token":
                access = c.value
            elif c.name == "aupd_refresh_token":
                refresh = c.value
        if not access:
            raise SystemExit("МЭШ не выдал токен: используйте python mes_login.py --tokens")
        return access, refresh or ""


async def finish(access, refresh):
    """Сохранить токены и проверить их запросом профиля; при неудаче вернуть прежний вход (если был)."""
    if not (access or "").strip():
        raise mes.MesError("токен пустой")
    mes.load()
    old = dict(mes.state)
    mes.set_tokens(access, refresh)
    try:
        return await mes.me(refresh=True)
    except Exception:
        mes.state.clear()
        mes.state.update(old)
        if old.get("access"):
            mes.save()
        else:
            try:
                os.remove(mes.FILE)
            except OSError:
                pass
        mes._cache.clear()
        mes._profile.clear()
        raise


async def main():
    if "--stdin" in sys.argv:
        access, refresh = sys.stdin.readline().strip(), sys.stdin.readline().strip()
    elif "--tokens" in sys.argv:
        access = getpass.getpass("aupd_token (ввод скрыт): ").strip()
        refresh = getpass.getpass("aupd_refresh_token (можно пусто, ввод скрыт): ").strip()
    else:
        access, refresh = await qr_login()
    try:
        p = await finish(access, refresh)
    except (mes.MesAuthError, mes.MesError) as e:
        print(f"Токены не подошли, прежний вход оставлен как был: {e}")
        raise SystemExit(1)
    print(f"Готово: {p['name'] or '?'}, {p['class_name'] or '?'}, {p['school'] or '?'}")


if __name__ == "__main__":
    asyncio.run(main())
