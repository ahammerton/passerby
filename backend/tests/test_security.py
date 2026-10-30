import asyncio
import os
import stat

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import config, main
from app.security import AdminAuth, host_allowed, same_origin
from tests.test_settings import client, env  # noqa: F401  (fixtures)


def test_local_hosts_are_allowed_and_public_names_are_not():
    for host in ("localhost:8000", "192.168.1.20:8000", "[::1]:8000", "10.0.0.2", "tracker.lan",
                 "pi.local:8000", "tracker.home.arpa", "box.internal", "server.tail1234.ts.net"):
        assert host_allowed(host, []), host
    # A public name pointed at the server (DNS rebinding) is refused unless listed.
    for host in ("evil.example.com", "evil.example.com:8000", "", "lan.example.com"):
        assert not host_allowed(host, []), host
    assert host_allowed("tracker.example.com", ["tracker.example.com"])
    assert host_allowed("anything.example.com", ["*"])


def test_same_origin():
    assert same_origin(None, "localhost:8000")  # not a browser
    assert same_origin("http://localhost:8000", "localhost:8000")
    assert same_origin("https://tracker.lan", "tracker.lan")
    assert not same_origin("http://evil.example.com", "localhost:8000")
    assert not same_origin("http://localhost:9000", "localhost:8000")
    assert not same_origin("null", "localhost:8000")


def test_requests_by_a_public_host_name_are_refused(client):
    assert client.get("/api/config").status_code == 200
    assert client.get("/api/config", headers={"host": "evil.example.com"}).status_code == 403


def test_changes_from_another_site_are_refused(client):
    evil = {"origin": "http://evil.example.com"}
    assert client.put("/api/settings", json={"home_name": "Hacked"}, headers=evil).status_code == 403
    assert client.delete("/api/settings", headers=evil).status_code == 403
    assert client.post("/api/receiver", json={"address": "10.0.0.1"}, headers=evil).status_code == 403
    # Reading the map data cross-site is left to the browser's same-origin rules.
    assert client.get("/api/config", headers=evil).status_code == 200
    ok = client.put("/api/settings", json={"home_name": "Cottage"}, headers={"origin": "http://localhost:8000"})
    assert ok.status_code == 200


def test_websocket_from_another_site_is_refused(client):
    # The test client always names its WebSocket host "testserver", so name the host here.
    local = {"host": "localhost:8000"}
    with client.websocket_connect("/ws", headers={**local, "origin": "http://localhost:8000"}) as ws:
        assert ws.receive_json()["type"] == "snapshot"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={**local, "origin": "http://evil.example.com"}) as ws:
            ws.receive_json()


def test_security_headers(client):
    headers = client.get("/").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
    # OpenStreetMap blocks tile requests that carry no Referer.
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_sessions(tmp_path):
    auth = AdminAuth("hunter2", tmp_path)
    token = auth.new_session(now=1000)
    assert auth.valid(token, now=2000)
    assert not auth.valid(token, now=1000 + 31 * 86400)  # expired
    assert not auth.valid(token[:-1] + ("0" if token[-1] != "0" else "1"), now=2000)  # tampered
    assert not auth.valid(None) and not auth.valid("junk")
    # Changing the password signs everyone out; the key file is owner-only.
    assert not AdminAuth("changed", tmp_path).valid(token, now=2000)
    assert stat.S_IMODE(os.stat(tmp_path / "secret.key").st_mode) == 0o600
    assert AdminAuth("", tmp_path).valid(None)  # no password set: everything allowed


def test_wrong_passwords_lock_out(tmp_path, monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    auth = AdminAuth("hunter2", tmp_path)
    for _ in range(10):
        assert not asyncio.run(auth.check_password("guess", "10.0.0.9"))
    with pytest.raises(HTTPException) as err:
        asyncio.run(auth.check_password("hunter2", "10.0.0.9"))
    assert err.value.status_code == 429
    assert asyncio.run(auth.check_password("hunter2", "10.0.0.8"))  # other addresses unaffected


def test_admin_password_guards_settings(client, env, monkeypatch):
    monkeypatch.setattr(main, "admin", AdminAuth("hunter2", env))
    assert client.get("/api/session").json() == {"required": True, "signed_in": False}
    assert client.get("/api/settings").status_code == 401
    assert client.put("/api/settings", json={"home_name": "Hacked"}).status_code == 401
    assert client.delete("/api/settings").status_code == 401
    assert client.post("/api/receiver", json={"address": "10.0.0.1"}).status_code == 401
    assert client.get("/api/config").status_code == 200  # the map stays open

    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    resp = client.post("/api/login", json={"password": "hunter2"})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/session").json()["signed_in"]
    assert client.put("/api/settings", json={"home_name": "Cottage"}).status_code == 200

    client.post("/api/logout")
    assert client.get("/api/settings").status_code == 401


def test_settings_file_is_owner_only(env):
    config.write_overrides(env, {"nrod_password": "secret"})
    assert stat.S_IMODE(os.stat(env / "settings.json").st_mode) == 0o600


def test_photo_links_must_be_https():
    from app.enrichment import parse_planespotters
    good = {"photos": [{"thumbnail_large": {"src": "https://t.plnspttrs.net/1.jpg"},
                        "link": "https://www.planespotters.net/photo/1", "photographer": "A"}]}
    assert parse_planespotters(good)["link"].startswith("https://")
    bad_link = {"photos": [{**good["photos"][0], "link": "javascript:alert(1)"}]}
    assert parse_planespotters(bad_link)["link"] is None
    bad_image = {"photos": [{**good["photos"][0], "thumbnail_large": {"src": "javascript:x"}}]}
    assert parse_planespotters(bad_image) is None


def test_frontend_is_revalidated(client):
    for path in ("/", "/app.js", "/style.css"):
        resp = client.get(path)
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-cache"
    etag = client.get("/app.js").headers["etag"]
    assert client.get("/app.js", headers={"if-none-match": etag}).status_code == 304
