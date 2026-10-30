import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config, main
from app.security import AdminAuth
from app.trains.stations import Station
from app.user_settings import SettingsUpdate

STATIONS = {"FEL": Station("FEL", "Feltham", 51.447858, -0.409758),
            "AFS": Station("AFS", "Ashford (Surrey)", 51.436585, -0.468077)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A clean environment: no aircraft sources, no trains, nothing from the developer's .env."""
    for key, value in {
        "DATA_DIR": str(tmp_path), "HOME_LAT": "51.5", "HOME_LON": "-0.12", "HOME_NAME": "Home",
        "RADIUS_KM": "150", "OPENSKY_ENABLED": "false", "ADSBLOL_ENABLED": "false",
        "AIRPLANESLIVE_ENABLED": "false", "READSB_ENABLED": "false", "ENRICHMENT_ENABLED": "false",
        "NROD_USERNAME": "", "NROD_PASSWORD": "", "DISTANCE_UNIT": "km", "TRAIN_WATCH": "", "TRAIN_BOARDS": "", "BODS_API_KEY": "", "BUS_DELAYS": "false", "BUS_DISRUPTIONS": "false", "NATIONAL_HIGHWAYS_API_KEY": "",
    }.items():
        monkeypatch.setenv(key, value)
    config.env_settings.cache_clear()
    config.get_settings.cache_clear()
    yield tmp_path
    config.env_settings.cache_clear()
    config.get_settings.cache_clear()


def test_overrides_beat_environment(env):
    config.write_overrides(env, {"home_name": "Cottage", "distance_unit": "mi"})
    s = config.reload_settings()
    assert (s.home_name, s.distance_unit, s.radius_km) == ("Cottage", "mi", 150)
    assert config.env_settings().home_name == "Home"
    config.write_overrides(env, {})
    assert not (env / "settings.json").exists()
    assert config.reload_settings().home_name == "Home"


def test_update_validation():
    assert SettingsUpdate(train_watch=" fel , afs ", train_boards="fel,FEL,hou").changes() == {
        "train_watch": "FEL,AFS", "train_boards": "FEL,HOU"}
    assert SettingsUpdate(train_watch="").changes() == {"train_watch": ""}
    for bad in ({"home_lat": 91}, {"radius_km": 1000}, {"distance_unit": "furlongs"},
                {"train_watch": "FEL"}, {"train_watch": "FEL,FEL"}, {"train_boards": "A,B,C,D,E"},
                {"data_dir": "/etc"}, {"nrod_password": "x" * 500}):
        with pytest.raises(ValidationError):
            SettingsUpdate(**bad)


@pytest.fixture
def client(env, monkeypatch):
    async def no_airports(*args):
        return []

    async def no_weather(*args):
        return []

    async def stations(*args):
        return STATIONS

    monkeypatch.setattr(main, "load_airports", no_airports)
    monkeypatch.setattr(main, "fetch_metars", no_weather)
    monkeypatch.setattr(main, "load_stations", stations)
    # No admin password, whatever the developer's .env says (tests that need one set their own).
    monkeypatch.setattr(main, "admin", AdminAuth("", env))
    # A local host name, as a browser on the home network would send.
    with TestClient(main.app, base_url="http://localhost:8000") as c:
        yield c


def test_settings_api_saves_and_restarts(client, env):
    body = client.get("/api/settings").json()
    assert body["values"]["home_name"] == "Home" and body["overridden"] == []
    assert body["secrets"] == {"nrod_password": False, "opensky_client_secret": False, "bods_api_key": False,
                               "national_highways_api_key": False}

    old_state = main.app.state.runtime.state
    resp = client.put("/api/settings", json={"home_name": "Cottage", "home_lat": 51.48, "home_lon": -0.4,
                                             "distance_unit": "mi", "radius_km": 150})
    assert resp.status_code == 200 and resp.json()["restarted"]
    # radius_km matches the .env value, so only real changes are stored.
    assert json.loads((env / "settings.json").read_text()) == {
        "home_name": "Cottage", "home_lat": 51.48, "home_lon": -0.4, "distance_unit": "mi"}
    # The running app picked them up.
    assert main.app.state.runtime.state is not old_state
    cfg = client.get("/api/config").json()
    assert cfg["home"] == {"lat": 51.48, "lon": -0.4, "name": "Cottage"}
    assert cfg["units"]["distance"] == "mi"

    # Saving the same values again doesn't restart.
    assert not client.put("/api/settings", json={"home_name": "Cottage"}).json()["restarted"]

    # Reset goes back to .env.
    assert client.delete("/api/settings").json()["restarted"]
    assert client.get("/api/config").json()["home"]["name"] == "Home"


def test_logins_are_checked_saved_and_never_returned(client, env, monkeypatch):
    checked = []

    async def fake_check(http, user, password):
        checked.append((user, password))
        return None if password == "right" else "Network Rail rejected that email and password."

    monkeypatch.setattr(main, "check_nrod", fake_check)
    # A wrong password is rejected and nothing is saved.
    resp = client.put("/api/settings", json={"nrod_username": "me@example.com", "nrod_password": "wrong"})
    assert resp.status_code == 422 and "rejected" in resp.json()["detail"]
    assert not (env / "settings.json").exists()

    # The right one is saved (privately), applied, and never sent back.
    resp = client.put("/api/settings", json={"nrod_username": "me@example.com", "nrod_password": "right"})
    body = resp.json()
    assert resp.status_code == 200 and body["restarted"]
    assert body["values"]["nrod_username"] == "me@example.com" and body["secrets"]["nrod_password"]
    assert "right" not in resp.text and "right" not in client.get("/api/settings").text
    assert (env / "settings.json").stat().st_mode & 0o777 == 0o600
    assert config.get_settings().nrod_password == "right"
    assert checked == [("me@example.com", "wrong"), ("me@example.com", "right")]

    # Changing only the username re-checks it with the saved password; clearing removes it.
    client.put("/api/settings", json={"nrod_username": "other@example.com"})
    assert checked[-1] == ("other@example.com", "right")
    assert not client.put("/api/settings", json={"nrod_password": ""}).json()["secrets"]["nrod_password"]


def test_settings_api_rejects_bad_values(client):
    assert client.put("/api/settings", json={"home_lat": 200}).status_code == 422
    resp = client.put("/api/settings", json={"train_boards": "FEL,ZZZ"})
    assert resp.status_code == 422 and "ZZZ" in resp.json()["detail"]


def test_nearby_stations(client):
    near = client.get("/api/stations", params={"lat": 51.44, "lon": -0.44, "radius_km": 10}).json()
    assert sorted(s["crs"] for s in near) == ["AFS", "FEL"]
    assert all("distance_km" in s for s in near)


def test_receiver_found_from_an_ip_when_saving(client, env, monkeypatch):
    async def fake_find(http, address):
        return ("http://192.168.1.50/dump1090/data/aircraft.json", 27, 10) if address == "192.168.1.50" else None

    monkeypatch.setattr(main, "find_receiver", fake_find)
    resp = client.put("/api/settings", json={"readsb_enabled": True, "readsb_url": "10.9.9.9"})
    assert resp.status_code == 422 and "No ADS-B receiver" in resp.json()["detail"]
    resp = client.put("/api/settings", json={"readsb_enabled": True, "readsb_url": "192.168.1.50",
                                             "online_feeds": "backup"})
    assert resp.status_code == 200
    assert resp.json()["values"]["readsb_url"] == "http://192.168.1.50/dump1090/data/aircraft.json"
    assert [s.name for s in main.app.state.runtime.state.sources][0] == "receiver"
    assert client.post("/api/receiver", json={"address": "192.168.1.50"}).json()["with_position"] == 10
