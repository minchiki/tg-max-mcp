"""Умскул без сети: вход, продление токена, чтение, защита адреса."""
import asyncio
import base64
import json
import os
import sys
import tempfile
import time

import httpx
import pytest

os.environ["UMS_FILE"] = os.path.join(tempfile.mkdtemp(), "ums.json")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import umschool  # noqa: E402

TOOLS = {}


def tool(write=False, **_):
    def deco(fn):
        TOOLS[fn.__name__] = fn
        return fn
    return deco


umschool.register(tool)


def run(c):
    return asyncio.run(c)


def jwt(exp):
    return "h." + base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=") + ".s"


@pytest.fixture
def net(monkeypatch):
    umschool.state.clear()
    if os.path.exists(umschool.FILE):
        os.remove(umschool.FILE)
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, dict(req.headers), req.content))
        p = req.url.path
        if p.endswith("/auth/login/"):
            body = json.loads(req.content)
            if body.get("password") != "good":
                return httpx.Response(400, json={"errors": [{"detail": "Неверный пароль"}]})
            return httpx.Response(200, json={"access": "oid-token", "refresh": "oid-ref"})
        if p.endswith("/o/authorize/"):
            q = req.url.params
            assert q["jwt_token"] == "oid-token" and q["code_challenge_method"] == "S256" and q["client_id"]
            return httpx.Response(302, headers={"location": q["redirect_uri"] + "&code=CODE1&state=" + q["state"]})
        if p.endswith("/o/token/"):
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            assert form["code"] == "CODE1" and form["grant_type"] == "authorization_code" and form["code_verifier"]
            return httpx.Response(200, json={"access_token": "oauth-tok"})
        if p.endswith("/v2/auth/o/login/"):
            if json.loads(req.content) != {"access": "oauth-tok"}:
                return httpx.Response(400, json={})
            return httpx.Response(200, json={"access": "acc-1", "refresh": "ref-1"})
        if p.endswith("/auth/refresh/"):
            return httpx.Response(200, json={"access": "acc-2"})
        if req.headers.get("authorization") == "Bearer expired":
            return httpx.Response(401, json={})
        if p.endswith("/auth/verify/"):
            return httpx.Response(200, json={"id": 5, "first_name": "Иван"})
        if p.endswith("/subscriptions/my/"):
            return httpx.Response(200, json=[{"title": "ОГЭ русский"}])
        return httpx.Response(404, json={})

    monkeypatch.setattr(umschool, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return calls


def test_login_saves_only_tokens_and_wrong_password_keeps_old(net):
    p = run(umschool.login("a@b.ru", "good"))
    assert p["first_name"] == "Иван"
    saved = open(umschool.FILE).read()
    assert "acc-1" in saved and "good" not in saved and "a@b.ru" not in saved
    with pytest.raises(umschool.UmsAuthError) as e:
        run(umschool.login("a@b.ru", "bad"))
    assert "Неверный пароль" in str(e.value) and "bad" not in str(e.value)
    assert umschool.state["access"] == "acc-1"


def test_expired_access_refreshes_and_keeps_refresh_token(net):
    umschool.set_tokens("expired", "ref-1")
    out = run(TOOLS["umschool_subscriptions"]())
    assert "ОГЭ русский" in out
    assert umschool.state["access"] == "acc-2" and umschool.state["refresh"] == "ref-1"


def test_keepalive_only_near_expiry(net):
    now = time.time()
    umschool.set_tokens(jwt(now + 7200), "ref-1")
    assert run(umschool.ensure_fresh(now=now)) is False
    umschool.set_tokens(jwt(now + 600), "ref-1")
    assert run(umschool.ensure_fresh(now=now)) is True


def test_raw_only_allows_cabinet_api(net):
    umschool.set_tokens("acc-1", "ref-1")
    with pytest.raises(umschool.UmsError):
        run(umschool.get("https://evil.example/api/x"))
    with pytest.raises(umschool.UmsError):
        run(TOOLS["umschool_raw"]("../admin"))


def test_not_logged_in_message(monkeypatch):
    umschool.state.clear()
    monkeypatch.setattr(umschool, "FILE", os.path.join(tempfile.mkdtemp(), "none.json"))
    assert "не выполнен" in run(TOOLS["umschool_status"]())
