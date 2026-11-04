"""Вход в Умскул по почте и паролю: сохраняет только токены в UMS_FILE, пароль нигде не хранится.

  python umschool_login.py            спросит почту и пароль (ввод пароля скрыт)
  python umschool_login.py --stdin    две строки (почта, пароль) из stdin
"""
import asyncio
import getpass
import sys

import umschool


async def main():
    if "--stdin" in sys.argv:
        email, password = sys.stdin.readline().strip(), sys.stdin.readline().strip()
    else:
        email = input("Почта от Умскула: ").strip()
        password = getpass.getpass("Пароль (ввод скрыт): ")
    try:
        p = await umschool.login(email, password)
    except (umschool.UmsAuthError, umschool.UmsError) as e:
        print(f"Не вышло: {e}")
        raise SystemExit(1)
    print("Готово, вход выполнен. " + umschool.dump(p, 300))


if __name__ == "__main__":
    asyncio.run(main())
