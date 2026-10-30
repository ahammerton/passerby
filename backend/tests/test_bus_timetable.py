import csv
import io
import zipfile
from datetime import date, datetime, time

import pytest

from app.bus_timetable import FORMAT, UK, BusTimetable, area_key, build, pick_region, simplify, stale

DAY = date(2026, 10, 29)  # a Thursday
HOME = (51.25, -0.60)


def at(hh_mm_ss, day=DAY):
    h, m, s = map(int, hh_mm_ss.split(":"))
    return datetime.combine(day, time(0), UK).timestamp() + h * 3600 + m * 60 + s


def table(name, header, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return name, out.getvalue()


# A route running north from home: stops A, B, C, D ~1.1 km apart (0.01° of latitude).
STOPS = [("A", "Alpha", 51.25), ("B", "Bravo", 51.26), ("C", "Charlie", 51.27), ("D", "Delta", 51.28)]


def gtfs(tmp_path):
    files = [
        table("agency.txt", ["agency_id", "agency_name", "agency_noc"], [["OP1", "Test Buses", "TEST"]]),
        table("routes.txt", ["route_id", "agency_id", "route_short_name"], [["R1", "OP1", "5"]]),
        table("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"],
              [[i, n, lat, -0.60] for i, n, lat in STOPS] + [["FAR", "Far away", 53.0, -2.0]]),
        table("trips.txt", ["route_id", "service_id", "trip_id", "trip_headsign", "shape_id"],
              [["R1", "WEEKDAY", "T1", "To Delta", "S1"], ["R1", "WEEKDAY", "LATE", "Night bus", ""],
               ["R1", "SUNDAY", "T2", "Sunday", ""], ["R1", "WEEKDAY", "ELSEWHERE", "Nowhere near", "S2"]]),
        # T1's road: a dog-leg east between B and C, plus points along straight stretches.
        table("shapes.txt", ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
              [["S1", lat, lon, i] for i, (lat, lon) in enumerate(
                  [(51.25, -0.60), (51.255, -0.60), (51.26, -0.60), (51.265, -0.59), (51.27, -0.60),
                   (51.275, -0.60), (51.28, -0.60)])] + [["S2", 53.0, -2.0, 0], ["S2", 53.1, -2.0, 1]]),
        table("stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
              [["T1", "08:00:00", "08:00:00", "A", 0], ["T1", "08:10:00", "08:12:00", "B", 1],
               ["T1", "08:20:00", "08:20:00", "C", 2], ["T1", "08:30:00", "08:30:00", "D", 3],
               ["LATE", "23:50:00", "23:50:00", "A", 0], ["LATE", "24:10:00", "24:10:00", "B", 1],
               ["T2", "08:00:00", "08:00:00", "A", 0], ["T2", "08:10:00", "08:10:00", "B", 1],
               ["ELSEWHERE", "08:00:00", "08:00:00", "FAR", 0], ["ELSEWHERE", "08:10:00", "08:10:00", "FAR", 1]]),
        table("calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                               "sunday", "start_date", "end_date"],
              [["WEEKDAY", 1, 1, 1, 1, 1, 0, 0, "20261001", "20261231"],
               ["SUNDAY", 0, 0, 0, 0, 0, 0, 1, "20261001", "20261231"]]),
        table("calendar_dates.txt", ["service_id", "date", "exception_type"], [["WEEKDAY", "20261225", 2]]),
        table("feed_info.txt", ["feed_version"], [["v1"]]),
    ]
    path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files:
            z.writestr(name, text)
    return path


@pytest.fixture
def tt(tmp_path):
    data = build(gtfs(tmp_path), *HOME, 10)
    return BusTimetable(data, 0, "south_east", area_key(*HOME, 10))


def bus(lat, recorded, departs="08:00:00", day=DAY, vid="v1", **extra):
    return {"id": vid, "operator": "TEST", "line": "5", "origin_ref": "A", "lat": lat, "lon": -0.60,
            "origin_departure": at(departs, day), "service_date": day.isoformat(), "recorded": recorded, **extra}


def test_build_keeps_only_trips_through_the_area(tmp_path):
    data = build(gtfs(tmp_path), *HOME, 10)
    assert set(data["trips"]) == {"T1", "LATE", "T2"} and data["version"] == "v1"
    assert "FAR" not in data["stops"]
    assert data["trips"]["T1"][:4] == ["TEST", "5", "WEEKDAY", "To Delta"]
    assert data["trips"]["LATE"][4][1] == ["B", 24 * 3600 + 600, 24 * 3600 + 600]  # past midnight
    # Only the paths of kept trips, without the points on straight stretches.
    assert data["trips"]["T1"][5] == "S1" and data["trips"]["T2"][5] is None
    assert data["shapes"] == {"S1": [[51.25, -0.6], [51.26, -0.6], [51.265, -0.59], [51.27, -0.6], [51.28, -0.6]]}


def test_simplify():
    straight = [(51.0 + i / 1000, -0.6) for i in range(10)]
    assert simplify(straight) == [[51.0, -0.6], [51.009, -0.6]]
    kink = [(51.0, -0.6), (51.001, -0.60005), (51.002, -0.6)]  # 3 m off the line: dropped
    assert len(simplify(kink)) == 2 and len(simplify(kink, tolerance_m=1)) == 3


def test_route(tt):
    r = tt.route(bus(51.265, at("08:19:00")))
    assert r["trip_id"] == "T1" and r["follows_roads"] and r["line"][2] == [51.265, -0.59]
    assert [s["name"] for s in r["stops"]] == ["Alpha", "Bravo", "Charlie", "Delta"]
    assert r["stops"][1] == {"name": "Bravo", "lat": 51.26, "lon": -0.6, "due": at("08:10:00")}
    # No published path: the stops joined in order.
    sunday = date(2026, 11, 1)
    plain = tt.route(bus(51.25, at("08:00:00", sunday), day=sunday))
    assert not plain["follows_roads"] and plain["line"] == [[51.25, -0.6], [51.26, -0.6]]
    assert tt.route({**bus(51.25, at("08:00:00")), "line": "6"}) is None


def test_matching_follows_the_calendar(tt):
    assert tt.match(bus(51.25, at("08:00:00"))) == ("T1", DAY)
    sunday = date(2026, 11, 1)
    assert tt.match(bus(51.25, at("08:00:00", sunday), day=sunday)) == ("T2", sunday)
    christmas = date(2026, 12, 25)  # a Friday, but the weekday service doesn't run
    assert tt.match(bus(51.25, at("08:00:00", christmas), day=christmas)) is None
    assert tt.match(bus(51.25, at("08:00:00"), departs="08:01:00")) is None
    assert tt.match({**bus(51.25, at("08:00:00")), "line": "6"}) is None
    # A placeholder service date is ignored; the departure time's date is used instead.
    assert tt.match({**bus(51.25, at("08:00:00")), "service_date": "0001-01-01"}) == ("T1", DAY)


def test_delay_between_stops(tt):
    # Halfway from B (leaves 08:12) to C (08:20) is due 08:16; seen there at 08:19 = 3 min late.
    p = tt.progress(bus(51.265, at("08:19:00")))
    assert p["delay_s"] == 180 and p["at_stop"] is None
    assert [s["name"] for s in p["next_stops"]] == ["Charlie", "Delta"]
    assert p["next_stops"][0]["due"] == at("08:20:00") and p["next_stops"][0]["expected"] == at("08:23:00")
    # Early: the delay shows, but stops are still expected on time (it'll wait).
    early = tt.progress(bus(51.265, at("08:14:00"), vid="v2"))
    assert early["delay_s"] == -120 and early["next_stops"][0]["expected"] == at("08:20:00")


def test_waiting_at_a_stop_is_on_time(tt):
    p = tt.progress(bus(51.26, at("08:11:00")))  # at B, which it leaves at 08:12
    assert p["delay_s"] == 0 and p["at_stop"] == "Bravo"
    assert p["next_stops"][0]["name"] == "Charlie"
    late = tt.progress(bus(51.26, at("08:15:00"), vid="v2"))  # still at B at 08:15, due 08:10
    assert late["delay_s"] == 300


def test_not_started_finished_and_implausible(tt):
    p = tt.progress(bus(51.28, at("07:40:00")))  # announcing the 08:00 while at the far end
    assert p["delay_s"] is None and p["starts_at"] == at("08:00:00") and p["first_stop"] == "Alpha"
    done = tt.progress(bus(51.28, at("08:31:00"), vid="v2"))
    assert done["finished"] and done["next_stops"] == [] and done["delay_s"] == 60
    assert tt.progress(bus(51.265, at("08:50:00"), vid="v3")) is None  # "34 min late": not believable
    assert tt.progress(bus(51.265, at("08:16:00"), vid="v4", lon=-0.50)) is None  # 7 km off the route


def test_journeys_after_midnight(tt):
    # The 23:50 runs into the next day; BODS keeps the day it started as the service date.
    p = tt.progress(bus(51.255, at("00:01:00", date(2026, 10, 30)), departs="23:50:00"))
    assert p["delay_s"] == 60 and p["next_stops"][0]["name"] == "Bravo"


def test_save_load_and_refresh_rules(tt, tmp_path):
    path = tmp_path / "t.json.gz"
    BusTimetable.save(path, {"version": "v1", "stops": tt.stops, "trips": tt.trips, "calendar": tt.calendar,
                             "exceptions": tt.exceptions}, "south_east", tt.area, 1000)
    loaded = BusTimetable.load(path)
    assert loaded.trips == tt.trips and loaded.built == 1000
    assert not stale(loaded, "south_east", tt.area, now=1000 + 86400)
    assert stale(loaded, "south_east", tt.area, now=1000 + 8 * 86400)  # weekly
    assert stale(loaded, "london", tt.area, now=1001) and stale(loaded, "south_east", "other", now=1001)
    assert stale(None, "south_east", tt.area)
    # A file from before route lines were kept is used, but rebuilt.
    older = BusTimetable({"stops": tt.stops, "trips": {k: v[:5] for k, v in tt.trips.items()},
                          "calendar": tt.calendar, "exceptions": tt.exceptions}, 1000, "south_east", tt.area, 1)
    assert older.shapes == {} and older.match(bus(51.25, at("08:00:00"))) == ("T1", DAY)
    assert not older.route(bus(51.25, at("08:00:00")))["follows_roads"]
    assert stale(older, "south_east", tt.area, now=1001) and loaded.format == FORMAT
    assert BusTimetable.load(tmp_path / "missing.json.gz") is None


def test_pick_region():
    assert pick_region(51.24, -0.57) == "south_east"  # Guildford
    assert pick_region(51.51, -0.13) == "london"
    assert pick_region(53.48, -2.24) == "north_west"  # Manchester
    assert pick_region(50.72, -3.53) == "south_west"  # Exeter


def test_old_timetable_used_while_rebuilding(tt, tmp_path, monkeypatch):
    """After the area changes (home or radius), the old timetable serves until the new one is built."""
    import asyncio
    from types import SimpleNamespace

    from app import main
    from app.bus_timetable import FILE

    BusTimetable.save(tmp_path / FILE, {"version": "v1", "stops": tt.stops, "trips": tt.trips,
                                        "calendar": tt.calendar, "exceptions": tt.exceptions},
                      "south_east", area_key(*HOME, 5), 1000)  # built for a 5 km radius
    downloading = asyncio.Event()

    async def slow_download(client, region, dest):
        downloading.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "download", slow_download)
    settings = SimpleNamespace(home_lat=HOME[0], home_lon=HOME[1], bus_radius_km=10, bus_region="auto",
                               data_dir=tmp_path)
    buses = SimpleNamespace(status={}, timetable=None)
    state = SimpleNamespace(settings=settings, buses=buses, client=None)

    async def run():
        task = asyncio.create_task(main.keep_bus_timetable(state))
        await asyncio.wait_for(downloading.wait(), 5)  # the radius changed: a rebuild started...
        assert buses.timetable is not None and buses.timetable.area == area_key(*HOME, 5)  # ...old one in use
        assert buses.timetable.match(bus(51.25, at("08:00:00"))) == ("T1", DAY)
        assert buses.status["timetable"]["state"] == "downloading"
        task.cancel()

    asyncio.run(run())
