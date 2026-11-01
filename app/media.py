"""Скачивание файлов по ссылкам: только публичные адреса, без DNS rebinding, с лимитом размера.

Имя хоста разрешается один раз, адрес проверяется, и соединение идёт именно на него
(Host и SNI — исходное имя, сертификат проверяется по имени). Иначе злоумышленник со своим DNS
мог бы отдать при проверке публичный IP, а при соединении — 127.0.0.1 или метаданные облака.
"""
import asyncio
import ipaddress
import mimetypes
import os
import socket
from urllib.parse import unquote, urljoin, urlparse

import httpx

MAX_BYTES = 50 * 1024 * 1024
FILE_META = {"openai/fileParams": ["file"]}  # ChatGPT передаёт загруженный файл в параметр file
UA = {"User-Agent": "Mozilla/5.0 (tg-max-mcp)"}


async def _public_ip(host):
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP)
    ips = [ipaddress.ip_address(sa[0]) for *_, sa in infos]
    if not ips or any(not ip.is_global for ip in ips):
        raise ValueError(f"Адрес {host} не публичный")
    return ips[0]


async def get(url, limit=MAX_BYTES):
    """GET по публичному адресу с ручными редиректами: (bytes, заголовки, итоговый url)."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as h:
        for _ in range(5):
            u = urlparse(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise ValueError("Нужна http(s)-ссылка")
            ip = await _public_ip(u.hostname)
            port = u.port or (443 if u.scheme == "https" else 80)
            netloc = f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"
            pinned = u._replace(netloc=netloc).geturl()
            headers = {**UA, "Host": u.netloc.rsplit("@", 1)[-1]}
            ext = {"sni_hostname": u.hostname} if u.scheme == "https" else {}
            async with h.stream("GET", pinned, headers=headers, extensions=ext) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers["location"])
                    continue
                r.raise_for_status()
                if int(r.headers.get("content-length") or 0) > limit:
                    raise ValueError(f"Файл больше {max(1, limit // 1048576)} МБ")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > limit:
                        raise ValueError(f"Файл больше {max(1, limit // 1048576)} МБ")
                return bytes(buf), r.headers, url
    raise ValueError("Слишком много редиректов")


async def fetch(src):
    """Файл для отправки: ссылка или файл из ChatGPT ({"download_url": ...}) → (bytes, имя, mime)."""
    name = None
    if isinstance(src, dict):
        name = src.get("file_name") or src.get("name")
        src = src.get("download_url") or src.get("url")
    if not isinstance(src, str):
        raise ValueError("Нужна http(s)-ссылка на файл или файл из чата")
    data, headers, url = await get(src)
    mime = headers.get("content-type", "").split(";")[0].strip()
    if not name:
        name = os.path.basename(unquote(urlparse(url).path)) or "file"
    if "." not in name:
        name += mimetypes.guess_extension(mime or "") or ""
    mime = mime if mime and mime != "application/octet-stream" else (mimetypes.guess_type(name)[0] or "")
    return data, name, mime
