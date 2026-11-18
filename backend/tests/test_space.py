import asyncio
import os
from pathlib import Path

import httpx
import pytest

from app.space import orbit
from app.space import tracker as space

# Real CelesTrak elements (18 November 2026): ISS, a module docked to it, and Tiangong.
TLE = (Path(__file__).parent / "data" / "space" / "stations.txt").read_text()
HOME = (51.47, -0.4543)  # Heathrow
T = 1795025000  # the expected values below are Skyfield's, for this moment


def iss():
    return next(s for s in orbit.parse_tle(TLE) if s.norad == 25544)


def test_parse_tle_skips_junk():
    sats = orbit.parse_tle("garbage\n" + TLE + "NAME ONLY\n1 broken line\n")
    assert [s.norad for s in sats] == [25544, 36086, 48274]
    assert sats[0].name == "ISS (ZARYA)"


def test_position_matches_skyfield():
    obs = orbit.Observer(*HOME)
    sat = iss()
    az, el, rng = obs.look(orbit.teme_to_ecef(sat.teme(T), T))
    assert el == pytest.approx(-41.02, abs=0.05) and az == pytest.approx(256.82, abs=0.05)
    assert rng == pytest.approx(8977.4, abs=5)
    w = sat.where(T)
    assert w["lat"] == pytest.approx(-5.139, abs=0.01) and w["lon"] == pytest.approx(-77.486, abs=0.01)
    assert 380 < w["alt_km"] < 440
    assert obs.sun_elevation(T) == pytest.approx(-17.52, abs=0.05)
    assert orbit.sunlit(sat.teme(T), T)


def test_passes_match_skyfield():
    # Skyfield's rise, culmination and set times for the next three passes.
    expected = [(1795026058, 1795026383, 1795026710), (1795031865, 1795032192, 1795032519),
                (1795037672, 1795037985, 1795038298)]
    got = orbit.passes(iss(), orbit.Observer(*HOME), T, 4)
    assert len(got) == 3
    for p, (rise, peak, set_) in zip(got, expected):
        assert abs(p["rise"]["time"] - rise) <= 2 and abs(p["set"]["time"] - set_) <= 2
        assert abs(p["max"]["time"] - peak) <= 5


def test_visible_pass_has_where_to_look():
    passes = orbit.passes(iss(), orbit.Observer(*HOME), T, 24)
    seen = [p for p in passes if p["visible"]]
    assert seen, "the ISS is visible from London on this date"
    for p in seen:
        assert p["appears"]["el"] >= orbit.VISIBLE_MIN_ELEVATION - 1
        assert p["rise"]["time"] <= p["appears"]["time"] <= p["vanishes"]["time"] <= p["set"]["time"]
        assert p["appears"]["dir"] in orbit.COMPASS and p["visible_max_el"] <= p["max"]["el"]
        assert p["appears"]["time"] <= p["visible_peak"]["time"] <= p["vanishes"]["time"]
    assert {p["why_not"] for p in passes if not p["visible"]} <= {"too low", "daylight", "shadow"}


def test_ground_track_crosses_date_line_cleanly():
    track = orbit.ground_track(iss(), T, 95)
    assert len(track) == 191 and all(-90 <= la <= 90 and -180 <= lo <= 180 for _, la, lo in track)
    assert track[0][0] == T and track[-1][0] == T + 95 * 60


def make_tracker(alerts="stations"):
    t = space.SpaceTracker(*HOME, alerts)
    t.set_elements(TLE, "")
    t.compute_passes(now=T)
    return t


def test_tracker_stations_and_snapshot():
    t = make_tracker()
    assert sorted(t.stations) == [25544, 48274]  # the docked module isn't a separate station
    snap = t.snapshot(now=T)
    iss_now = next(s for s in snap["stations"] if s["short"] == "ISS")
    assert iss_now["el"] < 0 and 7.5 < iss_now["speed_kms"] < 7.8
    assert iss_now["track"][0][0] == T - space.TRACK_BEHIND_MIN * 60
    assert all(p["visible"] for p in snap["visible_passes"])


def test_alerts_once_and_only_when_due():
    t = make_tracker()
    p = next(p for p in t.station_passes if p["visible"])
    start = p["appears"]["time"]
    assert t.new_alerts(now=start - 600) == []  # too early
    assert [a["norad"] for a in t.new_alerts(now=start - 120)] == [p["norad"]]
    assert t.new_alerts(now=start - 60) == []  # already sent
    assert make_tracker("off").new_alerts(now=start - 120) == []


def test_load_group_uses_cache_then_downloads(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.params["GROUP"])
        return httpx.Response(200, text=TLE)

    async def scenario(now):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await space.load_group(client, tmp_path, "stations", now=now)

    import time
    assert "ISS (ZARYA)" in asyncio.run(scenario(time.time()))
    assert calls == ["stations"]
    asyncio.run(scenario(time.time()))  # fresh copy on disk: no download
    assert calls == ["stations"]
    old = time.time() - space.REFRESH_SECONDS - 10
    os.utime(tmp_path / "tle_stations.txt", (old, old))
    asyncio.run(scenario(time.time()))
    assert calls == ["stations", "stations"]


def test_load_group_keeps_cache_when_celestrak_fails(tmp_path):
    (tmp_path / "tle_stations.txt").write_text(TLE)
    old = 0
    os.utime(tmp_path / "tle_stations.txt", (old, old))

    async def scenario():
        transport = httpx.MockTransport(lambda r: httpx.Response(503))
        async with httpx.AsyncClient(transport=transport) as client:
            return await space.load_group(client, tmp_path, "stations")

    assert "ISS (ZARYA)" in asyncio.run(scenario())


def test_station_passes_never_crowded_out():
    t = make_tracker()
    # Lots of bright satellites passing before the station does.
    first = next(p for p in t.station_passes if p["visible"])
    fake = {"norad": 1, "name": "X", "visible": True, "visible_max_el": 50,
            "appears": {"time": T + 60}, "vanishes": {"time": T + 300}, "max": {"el": 50}}
    t.satellite_passes = [fake] * 100
    visible = t.snapshot(now=T)["visible_passes"]
    assert any(p.get("station") and p["appears"]["time"] == first["appears"]["time"] for p in visible)
