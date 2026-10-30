import json
from datetime import datetime, timezone

import pytest

from app.trains.corpus import Location, parse_corpus
from app.trains.feed import _Listener
from app.trains.stations import Station, parse_stations, stations_within
from app.trains.tracker import TrainTracker, project_fraction
from app.trains.trust import headcode, parse_movement, train_class, uk_local_ms

# Real coordinates from the station dataset and STANOX codes from CORPUS.
GLD = Station("GLD", "Guildford", 51.2372, -0.581139)
WPL = Station("WPL", "Worplesdon", 51.287885, -0.582421)
WOK = Station("WOK", "Woking", 51.31847, -0.557814)
CLJ = Station("CLJ", "Clapham Junction", 51.464134, -0.170281)
STATIONS = {s.crs: s for s in (GLD, WPL, WOK, CLJ)}
CORPUS = {
    "87052": Location("87052", "Guildford", "GLD"),
    "87045": Location("87045", "Worplesdon", "WPL"),
    "86031": Location("86031", "Woking", "WOK"),
    "87219": Location("87219", "Clapham Junction", "CLJ"),
    "87050": Location("87050", "Guildford North Jn", None),  # a junction: no coordinates
}
# Halfway between Guildford and Worplesdon.
HOME = ((GLD.lat + WPL.lat) / 2, (GLD.lon + WPL.lon) / 2)


def uk(hh_mm: str, date: str = "2026-10-13") -> float:
    """Unix time of a UK wall-clock time (BST on this date)."""
    return datetime.fromisoformat(f"{date}T{hh_mm}:00+01:00").timestamp()


def trust_ms(hh_mm: str, date: str = "2026-10-13") -> str:
    """How TRUST encodes that wall-clock time: as if it were UTC."""
    return str(int(datetime.fromisoformat(f"{date}T{hh_mm}:00+00:00").timestamp() * 1000))


def movement(stanox, event, at, *, train="872G41MD13", nxt=None, run=None, status="ON TIME",
             variation="0", terminated=False):
    return {"header": {"msg_type": "0003"}, "body": {
        "train_id": train, "loc_stanox": stanox, "event_type": event, "actual_timestamp": trust_ms(at),
        "next_report_stanox": nxt or "", "next_report_run_time": run if run is not None else "",
        "variation_status": status, "timetable_variation": variation, "toc_id": "84",
        "train_terminated": "true" if terminated else "false", "platform": " 2",
    }}


def make_tracker(watch=None, radius_km=10):
    return TrainTracker(corpus=CORPUS, stations=STATIONS, home=HOME, radius_km=radius_km,
                        watch=watch, alert_seconds=60)


def positions(tracker, at):
    return {p["uid"]: p for p in tracker.snapshot(now=uk(at))[0]["positions"]}


# ---------- parsing ----------

def test_trust_times_are_uk_local():
    # In summer the raw value runs an hour ahead of the true instant...
    assert uk_local_ms(trust_ms("16:41")) == datetime(2026, 10, 13, 15, 41, tzinfo=timezone.utc).timestamp()
    # ...and in winter it matches UTC.
    assert uk_local_ms(trust_ms("16:41", "2026-12-01")) == datetime(2026, 12, 1, 16, 41, tzinfo=timezone.utc).timestamp()
    assert uk_local_ms("") is None


def test_parse_movement():
    m = parse_movement(movement("87052", "DEPARTURE", "10:00", nxt="87045", run="4",
                                status="LATE", variation="3")["body"])
    assert (m.stanox, m.event, m.next_stanox) == ("87052", "DEPARTURE", "87045")
    assert m.time == uk("10:00") and m.next_eta == uk("10:04")
    assert m.late_minutes == 3 and m.platform == "2" and not m.terminated
    early = parse_movement(movement("87052", "ARRIVAL", "10:00", status="EARLY", variation="2")["body"])
    assert early.late_minutes == -2 and early.next_eta is None
    assert parse_movement({"train_id": "x"}) is None


def test_headcode_and_class():
    assert headcode("872G41MD13") == "2G41"
    assert train_class("2G41") == "Stopping passenger"
    assert train_class("6M23") == "Freight (60 mph)"


def test_parse_corpus_prefers_station_rows():
    corpus = parse_corpus({"TIPLOCDATA": [
        {"STANOX": "87052", "TIPLOC": "GUILDFX", "3ALPHA": " ", "NLCDESC": "GUILDFORD SIDINGS"},
        {"STANOX": "87052", "TIPLOC": "GUILDFD", "3ALPHA": "GLD", "NLCDESC": "GUILDFORD"},
        {"STANOX": " ", "TIPLOC": "NOWHERE", "3ALPHA": "", "NLCDESC": "NOWHERE"},
    ]})
    assert corpus == {"87052": Location("87052", "Guildford", "GLD")}


# ---------- positions ----------

def test_train_between_stations_is_interpolated():
    t = make_tracker()
    t.ingest([movement("87052", "DEPARTURE", "10:00", nxt="87045", run="4")], now=uk("10:00"))
    p = positions(t, "10:02")["872G41MD13"]
    assert p["state"] == "between" and (p["from"], p["to"]) == ("GLD", "WPL")
    assert p["progress"] == pytest.approx(0.5)
    assert p["lat"] == pytest.approx((GLD.lat + WPL.lat) / 2)
    assert p["headcode"] == "2G41" and p["kind"] == "Stopping passenger" and not p["freight"]
    assert p["next"] == "Worplesdon" and p["last_report"] == "Guildford"


def test_arrival_and_overdue_and_stale():
    t = make_tracker()
    t.ingest([movement("87045", "ARRIVAL", "10:04")], now=uk("10:04"))
    assert positions(t, "10:05")["872G41MD13"]["state"] == "at"

    t.ingest([movement("87045", "DEPARTURE", "10:05", nxt="86031", run="5")], now=uk("10:05"))
    # Due at Woking 10:10 but no report yet: shown there as approaching, then dropped.
    overdue = positions(t, "10:12")["872G41MD13"]
    assert overdue["state"] == "approaching" and overdue["at"] == "WOK"
    assert positions(t, "10:21") == {}


def test_junction_reports_keep_the_last_station_as_start():
    t = make_tracker()
    t.ingest([movement("87052", "DEPARTURE", "10:00", nxt="87050", run="1")], now=uk("10:00"))
    # Next report point is a junction with no coordinates: briefly shown leaving GLD.
    assert positions(t, "10:00")["872G41MD13"]["state"] == "departing"
    # Passing the junction, now due at Worplesdon at 10:04.
    t.ingest([movement("87050", "DEPARTURE", "10:01", nxt="87045", run="3")], now=uk("10:01"))
    p = positions(t, "10:02")["872G41MD13"]
    assert (p["from"], p["to"]) == ("GLD", "WPL")
    assert p["progress"] == pytest.approx(0.5)


def test_terminated_cancelled_out_of_order_and_radius():
    t = make_tracker()
    t.ingest([movement("87052", "ARRIVAL", "10:00", terminated=True)], now=uk("10:00"))
    assert positions(t, "10:01")["872G41MD13"]["state"] == "terminated"
    assert positions(t, "10:03") == {}

    t.ingest([movement("87052", "DEPARTURE", "10:10", train="876M23XX13", nxt="87045", run="4")], now=uk("10:10"))
    # An older report arriving late doesn't move the train backwards.
    t.ingest([movement("86031", "ARRIVAL", "10:05", train="876M23XX13")], now=uk("10:11"))
    p = positions(t, "10:11")["876M23XX13"]
    assert p["state"] == "between" and p["freight"]
    t.ingest([{"header": {"msg_type": "0002"}, "body": {"train_id": "876M23XX13"}}], now=uk("10:12"))
    assert positions(t, "10:12") == {}

    # Trains outside the radius aren't shown.
    t.ingest([movement("87219", "ARRIVAL", "10:15", train="871A01XX13")], now=uk("10:15"))
    assert positions(t, "10:15") == {}
    assert t.snapshot(now=uk("10:15"))[0]["status"]["messages"] == 5


def test_reports_far_from_now_are_ignored():
    t = make_tracker()
    t.ingest([movement("87052", "ARRIVAL", "06:00")], now=uk("10:00"))
    assert t.trains == {}


# ---------- past home ----------

def test_watch_pass_time_and_alert_once():
    t = make_tracker(watch=("GLD", "WPL"))
    assert t.fraction == pytest.approx(0.5, abs=0.01)
    t.ingest([movement("87045", "DEPARTURE", "10:00", nxt="87052", run="4")], now=uk("10:00"))
    state, alerts = t.snapshot(now=uk("10:01") + 30)
    [p] = state["watch"]["passes"]
    # Heading WPL → GLD over 4 minutes with home halfway: passes at about 10:02.
    assert p["at_home"] == pytest.approx(uk("10:02"), abs=10)
    assert [a["uid"] for a in alerts] == ["872G41MD13"]
    assert t.snapshot(now=uk("10:01") + 40)[1] == []


def test_unknown_watch_code():
    with pytest.raises(ValueError, match="TRAIN_WATCH"):
        make_tracker(watch=("GLD", "ZZZ"))


def test_project_fraction():
    assert project_fraction(GLD, WPL, *HOME) == pytest.approx(0.5, abs=0.01)
    assert project_fraction(GLD, WPL, GLD.lat - 0.1, GLD.lon) == 0.0


# ---------- stations and feed ----------

def test_stations_parse_and_within():
    stations = parse_stations([
        {"stationName": "Guildford", "lat": 51.2372, "long": -0.581139, "crsCode": "GLD"},
        {"stationName": "Broken", "crsCode": "BRK"},
    ])
    assert list(stations) == ["GLD"]
    assert sorted(s.crs for s in stations_within(STATIONS, *HOME, 5)) == ["GLD", "WPL"]


def test_listener_hands_batches_to_tracker():
    received = []

    class Frame:
        body = json.dumps([movement("87052", "ARRIVAL", "10:00")])

    _Listener(received.append, {}).on_message(Frame())
    assert received[0][0]["body"]["loc_stanox"] == "87052"
