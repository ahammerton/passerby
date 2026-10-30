import json
from datetime import date, datetime

import pytest

from app.trains.corpus import Location, Tiploc
from app.trains.services import ServiceInfo
from app.trains.station_layout import parse_layout, platform_point, platform_refs
from app.trains.stations import Station
from app.trains.timetable import UK, Timetable
from app.trains.tracker import TrainTracker
from tests.test_settings import client, env  # noqa: F401  (fixtures)

GLD = Station("GLD", "Guildford", 51.2372, -0.581139)
WPL = Station("WPL", "Worplesdon", 51.287885, -0.582421)
STATIONS = {s.crs: s for s in (GLD, WPL)}
TIPLOCS = {"GUILDFD": Tiploc("GUILDFD", "87052", "Guildford", "GLD"),
           "WRPLSDN": Tiploc("WRPLSDN", "87045", "Worplesdon", "WPL"),
           "PORTSMH": Tiploc("PORTSMH", "88888", "Portsmouth Harbour", None)}
CORPUS = {"87052": Location("87052", "Guildford", "GLD"), "87045": Location("87045", "Worplesdon", "WPL")}
DAY = date(2026, 10, 15)


def uk(hh_mm):
    return datetime.fromisoformat(f"{DAY}T{hh_mm}:00").replace(tzinfo=UK).timestamp()


def trust_ms(hh_mm):
    return str(int(datetime.fromisoformat(f"{DAY}T{hh_mm}:00+00:00").timestamp() * 1000))


def way(id_, tags, points, type_="way"):
    return {"type": type_, "id": id_, "tags": tags, "geometry": [{"lat": a, "lon": b} for a, b in points]}


def test_platform_refs():
    assert platform_refs({"ref": "4;5"}) == ["4", "5"]
    assert platform_refs({"ref": "1a"}) == ["1A"]
    assert platform_refs({"name": "Platforms 2 and 3"}) == ["2", "3"]
    assert platform_refs({}) == []


def test_parse_layout():
    relation = {"type": "relation", "id": 9, "tags": {"railway": "platform", "ref": "8"},
                "members": [{"role": "outer", "geometry": [{"lat": 1, "lon": 1}, {"lat": 1, "lon": 2}, {"lat": 2, "lon": 2}]}]}
    layout = parse_layout([
        way(1, {"railway": "platform", "ref": "4;5", "covered": "yes"}, [(51.0, 0.0), (51.0, 0.01), (51.001, 0.01)]),
        way(1, {"railway": "platform", "ref": "4;5"}, [(51.0, 0.0), (51.0, 0.01)]),  # duplicate
        way(2, {"public_transport": "platform", "train": "yes", "ref": "1"}, [(51.0, 0.0), (51.0, 0.002)]),
        way(3, {"public_transport": "platform", "bus": "yes", "ref": "A"}, [(51.0, 0.0), (51.0, 0.002)]),  # bus stop
        way(4, {"railway": "rail"}, [(51.0, 0.0), (51.0, 0.02)]),
        way(5, {"railway": "rail", "service": "siding"}, [(51.0, 0.0), (51.0, 0.02)]),
        relation,
    ])
    assert [p["refs"] for p in layout["platforms"]] == [["1"], ["4", "5"], ["8"]]
    assert layout["platforms"][1]["covered"]
    assert [t["siding"] for t in layout["tracks"]] == [False, True]
    assert platform_point(layout["platforms"][0]) == pytest.approx([51.0, 0.001])


def make_tracker():
    tracker = TrainTracker(corpus=CORPUS, stations=STATIONS, home=(GLD.lat, GLD.lon), radius_km=20, watch=None,
                           alert_seconds=60, info=ServiceInfo(TIPLOCS, STATIONS), boards=[])
    sched = {"JsonScheduleV1": {
        "CIF_train_uid": "C11111", "CIF_stp_indicator": "P", "schedule_start_date": "2026-10-01",
        "schedule_end_date": "2026-12-12", "schedule_days_runs": "1111111", "atoc_code": "SW", "transaction_type": "Create",
        "schedule_segment": {"signalling_id": "1P47", "CIF_power_type": "EMU", "CIF_train_category": "XX", "schedule_location": [
            {"tiploc_code": "WRPLSDN", "departure": "1601", "public_departure": "1601", "platform": "1"},
            {"tiploc_code": "GUILDFD", "arrival": "1605", "departure": "1606", "public_arrival": "1605",
             "public_departure": "1606", "platform": "4"},
            {"tiploc_code": "PORTSMH", "arrival": "1700", "public_arrival": "1700"}]}}}
    tt = Timetable({"GUILDFD", "WRPLSDN"})
    tt.apply([json.dumps({"JsonTimetableV1": {"timestamp": uk("01:00"), "Metadata": {"sequence": 1}}}),
              json.dumps(sched, separators=(",", ":"))], DAY)
    tracker.set_timetable(tt)
    return tracker


def movement(stanox, event, at, platform="", nxt="", run=""):
    return {"header": {"msg_type": "0003"}, "body": {
        "train_id": "871P47MI15", "loc_stanox": stanox, "event_type": event, "actual_timestamp": trust_ms(at),
        "next_report_stanox": nxt, "next_report_run_time": run, "variation_status": "ON TIME",
        "timetable_variation": "0", "train_terminated": "false", "platform": platform}}


def test_arriving_train_uses_the_planned_platform_not_the_last_stations():
    t = make_tracker()
    # Left Worplesdon from platform 1, due at Guildford in 4 minutes.
    t.ingest([movement("87045", "DEPARTURE", "16:01", platform="1", nxt="87052", run="4")], now=uk("16:01"))
    view = t.station_view("GLD", now=uk("16:02"))
    [train] = view["trains"]
    assert train["status"] == "arriving" and train["destination"] == "Portsmouth Harbour"
    assert (train["platform"], train["platform_confirmed"]) == ("4", False)  # Worplesdon's "1" isn't carried over
    assert [d["platform"] for d in view["board"]["departures"]] == ["4"]


def test_train_at_platform_uses_the_reported_platform():
    t = make_tracker()
    # Actually ran into platform 5 instead of the planned 4.
    t.ingest([movement("87052", "ARRIVAL", "16:05", platform="5")], now=uk("16:05"))
    [train] = t.station_view("GLD", now=uk("16:05") + 20)["trains"]
    assert train["status"] == "at" and (train["platform"], train["platform_confirmed"]) == ("5", True)
    assert t.station_view("WPL", now=uk("16:05") + 20)["trains"] == []


def test_one_layout_download_per_station(client, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import main

    calls = []

    async def slow_layout(_client, _dir, st):
        calls.append(st.crs)
        await asyncio.sleep(0.05)
        return {"platforms": [], "tracks": []}

    monkeypatch.setattr(main, "load_station_layout", slow_layout)
    frm, gld = SimpleNamespace(crs="FRM"), SimpleNamespace(crs="GLD")

    async def run():
        # Four refreshes of Fareham while its layout is downloading, and one of Guildford.
        results = await asyncio.gather(*(main._station_layout(st) for st in (frm, frm, frm, frm, gld)))
        assert all(r == {"platforms": [], "tracks": []} for r in results)
        await main._station_layout(frm)  # finished: a new visit downloads (or reads the cache) again

    asyncio.run(run())
    assert calls == ["FRM", "GLD", "FRM"] and main._layout_loads == {}
