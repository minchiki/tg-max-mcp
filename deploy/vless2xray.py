"""Превращает подписку (URL) или ссылку vless:// в конфиг xray с SOCKS5 на 127.0.0.1:1080.

Использование: python3 vless2xray.py <URL подписки | vless://... | файл> [номер сервера]
Конфиг печатается в stdout.
"""
import base64
import html
import json
import re
import sys
import urllib.request
from urllib.parse import parse_qs, unquote, urlparse


def load(src):
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "v2rayN/6.0"})
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    elif src.startswith("vless://"):
        raw = src
    else:
        raw = open(src, encoding="utf-8").read()
    raw = raw.strip()
    if "://" not in raw:  # подписка в base64
        raw += "=" * (-len(raw) % 4)
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    # ссылки могут лежать прямо в HTML-странице: достаём регуляркой
    found = re.findall(r"(?:vless|vmess|trojan|ss|hysteria2|hy2|tuic)://[^\s\"'<>]+", html.unescape(raw))
    return list(dict.fromkeys(found))


def build(link):
    u = urlparse(link)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    net = q.get("type", "tcp")
    sec = q.get("security", "none")
    user = {"id": u.username, "encryption": q.get("encryption", "none")}
    if q.get("flow"):
        user["flow"] = q["flow"]
    ss = {"network": net, "security": sec}
    if sec == "reality":
        ss["realitySettings"] = {"serverName": q.get("sni", ""), "fingerprint": q.get("fp", "chrome"),
                                 "publicKey": q.get("pbk", ""), "shortId": q.get("sid", ""),
                                 "spiderX": unquote(q.get("spx", "/"))}
    elif sec == "tls":
        tls = {"serverName": q.get("sni") or u.hostname, "fingerprint": q.get("fp", "chrome")}
        if q.get("alpn"):
            tls["alpn"] = q["alpn"].split(",")
        ss["tlsSettings"] = tls
    if net == "ws":
        ss["wsSettings"] = {"path": unquote(q.get("path", "/")), "headers": {"Host": q.get("host", "")}}
    elif net == "grpc":
        ss["grpcSettings"] = {"serviceName": q.get("serviceName", "")}
    elif net in ("xhttp", "splithttp"):
        ss["network"] = "xhttp"
        ss["xhttpSettings"] = {"path": unquote(q.get("path", "/")), "host": q.get("host", ""),
                               "mode": q.get("mode", "auto")}
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "127.0.0.1", "port": 1080, "protocol": "socks",
                      "settings": {"udp": True}}],
        "outbounds": [{"protocol": "vless",
                       "settings": {"vnext": [{"address": u.hostname, "port": u.port or 443,
                                               "users": [user]}]},
                       "streamSettings": ss}],
    }


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    links = load(sys.argv[1])
    vless = [x for x in links if x.startswith("vless://")]
    if not vless:
        schemes = sorted({x.split("://")[0] for x in links if "://" in x})
        sys.exit(f"vless-ссылок нет. Найдены протоколы: {schemes or 'ничего'}. Пришлите формат подписки.")
    names = [unquote(x.split("#", 1)[1]) if "#" in x else x[:40] for x in vless]
    for i, n in enumerate(names):
        print(f"[{i}] {n}", file=sys.stderr)
    pick = sys.argv[2] if len(sys.argv) > 2 else "0"
    if pick.isdigit():
        idx = int(pick)
    else:  # часть названия, например "Финляндия"
        idx = next((i for i, n in enumerate(names) if pick.lower() in n.lower()), None)
        if idx is None:
            sys.exit(f"Сервер с названием '{pick}' не найден")
    print(f"выбран: [{idx}] {names[idx]}", file=sys.stderr)
    print(json.dumps(build(vless[idx]), indent=2, ensure_ascii=False))


main()
