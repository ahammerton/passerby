import sqlite3

from app.models import Aircraft, Route, RouteEnd
from app.store import SightingStore

DAY = 86400


def plane(icao, **kw):
    return Aircraft(icao24=icao, source="t", last_seen=0, distance_km=kw.pop("distance_km", 5.0), **kw)


def test_old_database_gains_new_columns(tmp_path):
    path = tmp_path / "s.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE sightings (id INTEGER PRIMARY KEY, icao24 TEXT NOT NULL, callsign TEXT,"
                     " registration TEXT, type_code TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL,"
                     " closest_km REAL NOT NULL, max_altitude_ft REAL, was_overhead INTEGER NOT NULL DEFAULT 0);"
                     "INSERT INTO sightings VALUES (1, 'abc', 'BAW1', NULL, 'A320', 10, 20, 3.0, 1000, 1);")
    db.close()
    store = SightingStore(path)
    [row] = store.recent()
    assert row["callsign"] == "BAW1" and row["aircraft_name"] is None and row["interest_level"] is None
    assert store.summary(0, 100)["aircraft"]["sightings"] == 1


def test_records_route_airline_and_most_serious_interest(tmp_path):
    store = SightingStore(tmp_path / "s.db")
    route = Route(airline="British Airways", origin=RouteEnd(iata="LHR", city="London"),
                  destination=RouteEnd(icao="EDDB", name="Berlin Brandenburg"))
    a = plane("abc", callsign="BAW986", type_code="A320", aircraft_name="Airbus A320", airline_name="British Airways",
              route=route, interest={"level": "rare", "reason": "Rare here"})
    store.record([a], now=1000)
    store.record([a.model_copy(update={"interest": {"level": "emergency", "reason": "Radio failure"}})], now=1010)
    store.record([a.model_copy(update={"interest": {"level": "military", "reason": "Military"}})], now=1020)
    [row] = store.recent()
    assert row["origin"] == "LHR (London)" and row["destination"] == "EDDB (Berlin Brandenburg)"
    assert row["airline_name"] == "British Airways" and row["aircraft_name"] == "Airbus A320"
    assert (row["interest_level"], row["interest_reason"]) == ("emergency", "Radio failure")


def test_summary_counts_and_period(tmp_path):
    store = SightingStore(tmp_path / "s.db")
    now = 100 * DAY
    store.record([plane("aaa", callsign="BAW1", type_code="A320", overhead=True),
                  plane("bbb", callsign="BAW22", type_code="a320"),
                  plane("ccc", callsign="GABCD", type_code="C172")], now=now - 3600)
    store.record([plane("ddd", callsign="EZY1", type_code="A319")], now=now - 10 * DAY)  # outside 7 days
    s = store.summary(now - 7 * DAY, now)["aircraft"]
    assert (s["sightings"], s["aircraft"], s["overhead"]) == (3, 3, 1)
    assert s["airlines"] == [{"code": "BAW", "name": None, "aircraft": 2}]  # GABCD isn't an airline callsign
    assert s["types"][0] == {"code": "A320", "name": None, "aircraft": 2}
    assert sum(h["sightings"] for h in s["by_hour"]) == 3 and len(s["by_hour"]) == 24
    assert [o["icao24"] for o in s["overhead_list"]] == ["aaa"]
    assert store.summary(0, now)["aircraft"]["aircraft"] == 4


def test_train_passes(tmp_path):
    store = SightingStore(tmp_path / "s.db")
    store.record_train_passes([
        {"uid": "1", "headcode": "1A23", "operator": "South Western Railway", "destination": "London Waterloo",
         "late_minutes": 3, "freight": False, "at_home": 1000},
        {"uid": "2", "headcode": "6M45", "operator": None, "freight": True, "at_home": 1100, "late_minutes": 0},
    ])
    t = store.summary(0, 2000)["trains"]
    assert t["passes"] == 2 and t["freight"] == 1 and t["late_share"] == 0.5
    assert {o["name"] for o in t["operators"]} == {"South Western Railway", "Freight"}
    assert t["recent"][0]["headcode"] == "6M45"
    assert store.summary(1050, 2000)["trains"]["passes"] == 1
