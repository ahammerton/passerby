import time

import pytest

from app.aggregator import Aggregator
from app.airports import nearest_airports, parse_airports
from app.geo import bearing_deg, bounding_box, compass, haversine_km
from app.models import Aircraft
from app.sources.opensky import parse_state
from app.sources.readsb import parse_aircraft
from app.store import SightingStore

HOME = (51.47, -0.4543)  # Heathrow


def test_haversine_known_distance():
    # Heathrow to JFK is ~5540 km.
    assert haversine_km(51.47, -0.4543, 40.6413, -73.7781) == pytest.approx(5540, rel=0.01)


def test_bearing_and_compass():
    assert bearing_deg(0, 0, 1, 0) == pytest.approx(0)
    assert bearing_deg(0, 0, 0, 1) == pytest.approx(90)
    assert compass(350) == "N"
    assert compass(225) == "SW"


def test_bounding_box_contains_radius():
    lamin, lomin, lamax, lomax = bounding_box(*HOME, 100)
    assert haversine_km(lamin, HOME[1], *HOME) == pytest.approx(100, rel=0.01)
    assert haversine_km(HOME[0], lomax, *HOME) == pytest.approx(100, rel=0.01)


def test_parse_opensky_state_converts_units():
    state = ["4ca7b4", "RYR12AB ", "Ireland", 1, 1700000000, -0.5, 51.5, 1000.0,
             False, 100.0, 270.0, 5.0, None, 1050.0, "1234", False, 0, 4]
    a = parse_state(state)
    assert a.icao24 == "4ca7b4"
    assert a.callsign == "RYR12AB"
    assert a.altitude_ft == pytest.approx(3280.84)
    assert a.speed_kt == pytest.approx(194.4, rel=0.01)
    assert a.vertical_rate_fpm == pytest.approx(984.25, rel=0.01)
    assert a.category == "A3"
    assert a.position_time == 1.0 and a.last_seen == 1700000000


def test_parse_readsb_handles_ground_and_tisb():
    now = 1700000000.0
    a = parse_aircraft({"hex": "40621D", "flight": "BAW1  ", "alt_baro": "ground",
                        "lat": 51.47, "lon": -0.45, "seen": 1.5, "r": "G-ABCD"}, now)
    assert a.on_ground and a.altitude_ft == 0
    assert a.icao24 == "40621d" and a.registration == "G-ABCD"
    assert a.last_seen == now - 1.5
    assert a.position_time is None  # no seen_pos: position age unknown
    moving = parse_aircraft({"hex": "40621d", "lat": 51.5, "lon": -0.4, "seen": 0.2, "seen_pos": 2.0}, now)
    assert moving.position_time == now - 2.0
    assert parse_aircraft({"hex": "~abc123"}, now) is None


def make_aircraft(icao, source, lat, lon, **kw):
    return Aircraft(icao24=icao, source=source, last_seen=kw.pop("last_seen", time.time()),
                    lat=lat, lon=lon, **kw)


def make_aggregator():
    return Aggregator(home_lat=HOME[0], home_lon=HOME[1], radius_km=100, alert_radius_km=10,
                      alert_min_altitude_ft=300, stale_after=60, priorities={"readsb": 10, "opensky": 0})


def test_aggregator_prefers_higher_priority_and_fills_gaps():
    agg = make_aggregator()
    agg.update("opensky", [make_aircraft("abc", "opensky", 51.5, -0.5, origin_country="UK")])
    agg.update("readsb", [make_aircraft("abc", "readsb", 51.48, -0.46, registration="G-TEST")])
    [a] = agg.snapshot()
    assert a.source == "readsb"
    assert a.origin_country == "UK" and a.registration == "G-TEST"
    assert a.overhead


def test_aggregator_filters_radius_stale_and_sorts():
    agg = make_aggregator()
    now = time.time()
    agg.update("opensky", [
        make_aircraft("far", "opensky", 55.0, -0.45),  # ~390 km away
        make_aircraft("old", "opensky", 51.5, -0.45, last_seen=now - 120),
        make_aircraft("mid", "opensky", 51.8, -0.45),
        make_aircraft("near", "opensky", 51.5, -0.45),
    ])
    assert [a.icao24 for a in agg.snapshot(now)] == ["near", "mid"]


def test_low_aircraft_are_not_overhead():
    agg = make_aggregator()
    agg.update("opensky", [
        make_aircraft("taxi", "opensky", 51.48, -0.46, altitude_ft=-50),
        make_aircraft("climb", "opensky", 51.48, -0.46, altitude_ft=1200),
    ])
    overhead = {a.icao24: a.overhead for a in agg.snapshot()}
    assert overhead == {"taxi": False, "climb": True}


def test_new_overhead_fires_once():
    agg = make_aggregator()
    agg.update("opensky", [make_aircraft("abc", "opensky", 51.48, -0.46)])
    assert len(agg.new_overhead(agg.snapshot())) == 1
    assert agg.new_overhead(agg.snapshot()) == []


def test_store_merges_visits(tmp_path):
    store = SightingStore(tmp_path / "s.db")
    a = make_aircraft("abc", "opensky", 51.48, -0.46, altitude_ft=3000)
    store.record([a.model_copy(update={"distance_km": 20.0})], now=1000)
    store.record([a.model_copy(update={"distance_km": 5.0, "overhead": True})], now=1010)
    [row] = store.recent()
    assert row["closest_km"] == 5.0 and row["was_overhead"] == 1
    # A long gap starts a new visit.
    store.record([a.model_copy(update={"distance_km": 30.0})], now=1010 + 3600)
    assert len(store.recent()) == 2
    assert store.stats(0) == {"visits": 2, "aircraft": 1, "overhead": 1}


def test_parse_airports_and_nearest():
    lines = [
        '507,"London Heathrow Airport","London","United Kingdom","LHR","EGLL",51.4706,-0.461941,83,0,"E","Europe/London","airport","OurAirports"',
        '1,"Heliport","X","Y",\\N,\\N,51.0,0.0,0,0,"E","Z","airport","OurAirports"',
        '2,"Station","X","Y",\\N,"ABCD",51.0,0.0,0,0,"E","Z","station","OurAirports"',
    ]
    airports = parse_airports(lines)
    assert [a.icao for a in airports] == ["EGLL"]
    [near] = nearest_airports(airports, *HOME, radius_km=50)
    assert near["iata"] == "LHR" and near["distance_km"] < 1


def test_sighting_store_shared_between_threads(tmp_path):
    # Snapshots are recorded in a worker thread while /api/history and /api/stats read from
    # request threads; unguarded, SQLite crashes the whole process.
    import threading

    from app.store import SightingStore

    store = SightingStore(tmp_path / "s.db")
    planes = [Aircraft(icao24=f"{i:06x}", source="t", last_seen=0, distance_km=1.0) for i in range(50)]
    errors = []

    def hammer(work):
        try:
            for i in range(200):
                work(i)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(lambda i: store.record(planes, now=i),)),
               threading.Thread(target=hammer, args=(lambda i: store.recent(100),)),
               threading.Thread(target=hammer, args=(lambda i: store.stats(0),))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.stats(0)["aircraft"] == 50
    store.close()
