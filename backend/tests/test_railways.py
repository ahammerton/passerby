import asyncio

import httpx
import pytest

from app.trains.corpus import Location
from app.trains.railways import RailNetwork, fraction_along, load_railways, point_along
from app.trains.stations import Station
from app.trains.tracker import TrainTracker

# Two stations 0.02° of longitude apart; the track between them bends north via a corner.
A = Station("AAA", "Alpha", 51.0, -0.60)
B = Station("BBB", "Bravo", 51.0, -0.58)
CORNER = (51.01, -0.59)


def way(way_id, nodes, points, **tags):
    return {"type": "way", "id": way_id, "nodes": nodes, "tags": {"railway": "rail", **tags},
            "geometry": [{"lat": lat, "lon": lon} for lat, lon in points]}


ELEMENTS = [
    way(1, [1, 2], [(A.lat, A.lon), CORNER]),
    way(2, [2, 3], [CORNER, (B.lat, B.lon)]),
    # A siding straight across would be a shortcut; it must be ignored.
    way(3, [1, 3], [(A.lat, A.lon), (B.lat, B.lon)], service="siding"),
]


def test_route_follows_track_not_siding():
    net = RailNetwork(ELEMENTS)
    assert net.path(A, B) == [(A.lat, A.lon), CORNER, (B.lat, B.lon)]
    assert net.path(B, A) == [(B.lat, B.lon), CORNER, (A.lat, A.lon)]
    # Only the running lines are drawn.
    assert len(net.lines) == 2


def test_point_and_fraction_along():
    path = [(A.lat, A.lon), CORNER, (B.lat, B.lon)]
    # Halfway by distance is the corner (both legs are the same length).
    assert point_along(path, 0.5) == pytest.approx(CORNER)
    assert point_along(path, 0) == (A.lat, A.lon) and point_along(path, 1) == (B.lat, B.lon)
    f, off = fraction_along(path, CORNER[0] + 0.001, CORNER[1])
    assert f == pytest.approx(0.5, abs=0.01) and off < 0.2


def test_stations_off_the_network_and_long_detours():
    net = RailNetwork(ELEMENTS)
    far = Station("FAR", "Far away", 52.0, -0.6)
    assert net.path(A, far) is None
    # A route over 2.5x the straight distance is a detour, not the real line.
    loop = RailNetwork([way(1, [1, 2, 3], [(A.lat, A.lon), (51.05, -0.59), (B.lat, B.lon)])])
    assert loop.path(A, B) is None


def test_tracker_places_trains_on_the_track():
    corpus = {"11111": Location("11111", "Alpha", "AAA"), "22222": Location("22222", "Bravo", "BBB")}
    tracker = TrainTracker(corpus=corpus, stations={"AAA": A, "BBB": B}, home=CORNER, radius_km=10,
                           watch=("AAA", "BBB"), alert_seconds=60, rail=RailNetwork(ELEMENTS))
    # Home sits on the corner: halfway along the track, right next to it.
    assert tracker.fraction == pytest.approx(0.5, abs=0.01) and tracker.offset_km < 0.1
    tracker.ingest([{"header": {"msg_type": "0003"}, "body": {
        "train_id": "872A01XX13", "loc_stanox": "11111", "event_type": "DEPARTURE",
        "actual_timestamp": "1760004000000", "next_report_stanox": "22222", "next_report_run_time": "4",
        "variation_status": "ON TIME", "timetable_variation": "0", "train_terminated": "false",
    }}], now=1760000400)
    state, _ = tracker.snapshot(now=1760000400 + 120)
    [p] = state["positions"]
    assert p["on_track"] and (p["lat"], p["lon"]) == pytest.approx(CORNER)
    assert state["watch"]["track"] == [(A.lat, A.lon), CORNER, (B.lat, B.lon)]


def test_load_railways_caches_and_falls_back(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "overpass-api.de":
            return httpx.Response(502)  # overloaded: try the next mirror
        return httpx.Response(200, json={"elements": ELEMENTS})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await load_railways(client, tmp_path, 51.0, -0.59, 5)
            second = await load_railways(client, tmp_path, 51.0, -0.59, 5)
            return first, second

    first, second = asyncio.run(scenario())
    assert len(first.lines) == len(second.lines) == 2
    # One failed server, one success, then served from the cache.
    assert calls == ["overpass-api.de", "overpass.kumi.systems"]


def test_track_arriving_after_startup_reroutes_watch():
    tracker = TrainTracker(corpus={}, stations={"AAA": A, "BBB": B}, home=CORNER, radius_km=10,
                           watch=("AAA", "BBB"), alert_seconds=60)
    # No track yet: a straight line, with home well off it.
    assert tracker.snapshot()[0]["watch"]["track"] == [[A.lat, A.lon], [B.lat, B.lon]]
    assert tracker.offset_km > 1
    tracker.set_rail(RailNetwork(ELEMENTS))
    assert tracker.snapshot()[0]["watch"]["track"] == [(A.lat, A.lon), CORNER, (B.lat, B.lon)]
    assert tracker.offset_km < 0.1 and tracker.fraction == pytest.approx(0.5, abs=0.01)


def test_rail_lines_are_retried_until_available(monkeypatch):
    from types import SimpleNamespace

    from app import main

    attempts, sleeps = [], []

    async def fake_load(*args):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("all Overpass servers busy")
        return RailNetwork(ELEMENTS)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main, "load_railways", fake_load)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    tracker = TrainTracker(corpus={}, stations={"AAA": A, "BBB": B}, home=CORNER, radius_km=10,
                           watch=None, alert_seconds=60)
    settings = SimpleNamespace(data_dir=None, home_lat=0, home_lon=0, train_radius_km=5)
    asyncio.run(main.load_rail_lines(SimpleNamespace(settings=settings, client=None, trains=tracker)))
    assert len(attempts) == 3 and sleeps == [60, 120]
    assert tracker.rail is not None
