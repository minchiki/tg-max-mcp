"""Файлы для отправки: URL или файл из ChatGPT ({"download_url": ...}). Только публичные адреса."""
import asyncio
import ipaddress
import mimetypes
import os
import socket
from urllib.parse import unquote, urlparse

import httpx

MAX_BYTES = 50 * 1024 * 1024
FILE_META = {"openai/fileParams": ["file"]}  # ChatGPT передаёт загруженный файл в параметр file


async def _check_public(host):
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    for *_, sa in infos:
        ip = ipaddress.ip_address(sa[0])
        if not ip.is_global:
            raise ValueError(f"Адрес {host} не публичный")


async def fetch(src):
    """Скачивает файл: возвращает (bytes, имя, mime)."""
    name = None
    if isinstance(src, dict):
        name = src.get("file_name") or src.get("name")
        src = src.get("download_url") or src.get("url")
    if not isinstance(src, str) or urlparse(src).scheme not in ("http", "https"):
        raise ValueError("Нужна http(s)-ссылка на файл или файл из чата")
    url = src
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "Mozilla/5.0 (tg-mcp)"}) as h:
        for _ in range(5):  # редиректы вручную, чтобы проверить каждый адрес
            await _check_public(urlparse(url).hostname)
            r = await h.get(url)
            if r.is_redirect:
                url = str(r.next_request.url)
                continue
            r.raise_for_status()
            break
        else:
            raise ValueError("Слишком много редиректов")
    data = r.content
    if len(data) > MAX_BYTES:
        raise ValueError("Файл больше 50 МБ")
    mime = r.headers.get("content-type", "").split(";")[0].strip()
    if not name:
        name = os.path.basename(unquote(urlparse(url).path)) or "file"
    if "." not in name:
        name += mimetypes.guess_extension(mime or "") or ""
    mime = mime if mime and mime != "application/octet-stream" else (mimetypes.guess_type(name)[0] or "")
    return data, name, mime
