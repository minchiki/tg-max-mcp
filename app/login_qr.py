"""Вход в Telegram по QR (без кода из SMS). Запускается один раз на сервере."""
import asyncio

import qrcode
from telethon.errors import SessionPasswordNeededError

from tgcfg import make_client


async def main():
    c = make_client()
    await c.connect()
    if not await c.is_user_authorized():
        qr = await c.qr_login()
        while True:
            q = qrcode.QRCode(border=1)
            q.add_data(qr.url)
            q.make()
            q.print_ascii(invert=True)
            print("Telegram на телефоне: Настройки > Устройства > Подключить устройство > сканировать")
            try:
                await qr.wait(timeout=30)
                break
            except asyncio.TimeoutError:
                await qr.recreate()
            except SessionPasswordNeededError:
                await c.sign_in(password=input("Пароль 2FA: "))
                break
    me = await c.get_me()
    print("вошли как", me.username or me.first_name)
    await c.disconnect()


asyncio.run(main())
