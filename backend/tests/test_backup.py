import json

from app import main
from app.security import AdminAuth
from tests.test_settings import client, env  # noqa: F401  (fixtures)


def backup(client, **params):
    resp = client.get("/api/settings/backup", params=params)
    return resp, (resp.json() if resp.status_code == 200 else None)


def test_backup_and_restore_round_trip(client, monkeypatch):
    async def ok(*args):
        return None

    monkeypatch.setattr(main, "check_nrod", ok)
    client.put("/api/settings", json={"home_name": "Cottage", "distance_unit": "mi", "bus_min_zoom": 14,
                                      "nrod_username": "me@example.com", "nrod_password": "secret"})
    resp, body = backup(client)
    assert resp.headers["content-disposition"].startswith('attachment; filename="passerby-settings-')
    assert body["app"] == "passerby" and body["format"] == 1 and not body["includes_logins"]
    assert body["settings"]["home_name"] == "Cottage" and body["settings"]["bus_min_zoom"] == 14
    assert "nrod_password" not in body["settings"]  # no logins unless asked (and protected)

    client.put("/api/settings", json={"home_name": "Elsewhere", "distance_unit": "km", "bus_min_zoom": 12})
    restored = client.post("/api/settings/restore", json=body).json()
    assert restored["restarted"] and restored["values"]["home_name"] == "Cottage"
    assert restored["values"]["distance_unit"] == "mi" and restored["values"]["bus_min_zoom"] == 14
    # The backup had no password, so the current one stays.
    assert restored["secrets"]["nrod_password"] is True
    assert any("current ones were kept" in n for n in restored["notes"])


def test_logins_only_in_backups_behind_a_password(client, env, monkeypatch):
    resp, _ = backup(client, secrets="true")
    assert resp.status_code == 403 and "ADMIN_PASSWORD" in resp.json()["detail"]

    monkeypatch.setattr(main, "admin", AdminAuth("hunter2", env))
    assert backup(client)[0].status_code == 401  # signed out
    client.post("/api/login", json={"password": "hunter2"})
    assert client.get("/api/settings").json()["admin_password_set"] is True
    client.put("/api/settings", json={"opensky_client_id": "", "contact": "me@example.com"})
    _, body = backup(client, secrets="true")
    assert body["includes_logins"] and "nrod_password" in body["settings"]

    client.post("/api/logout")
    assert client.post("/api/settings/restore", json=body).status_code == 401


def test_restore_accepts_backups_from_before_the_rename(client):
    _, body = backup(client)
    assert client.post("/api/settings/restore", json={**body, "app": "transport-tracker"}).status_code == 200


def test_restore_rejects_bad_files(client):
    _, body = backup(client)
    assert client.post("/api/settings/restore", json={**body, "app": "something-else"}).status_code == 422
    assert client.post("/api/settings/restore", json={**body, "format": 99}).status_code == 422
    bad = {**body, "settings": {**body["settings"], "home_lat": 200}}
    resp = client.post("/api/settings/restore", json=bad)
    assert resp.status_code == 422 and "home_lat" in resp.json()["detail"]
    assert client.post("/api/settings/restore", json={"settings": {}}).status_code == 422  # not a backup
    # Settings from a newer version are skipped, with a note.
    newer = {**body, "settings": {**body["settings"], "future_option": True, "home_name": "New"}}
    resp = client.post("/api/settings/restore", json=newer).json()
    assert resp["values"]["home_name"] == "New" and any("future_option" in n for n in resp["notes"])


def test_restore_from_another_site_is_refused(client):
    _, body = backup(client)
    resp = client.post("/api/settings/restore", json=body, headers={"origin": "http://evil.example.com"})
    assert resp.status_code == 403
