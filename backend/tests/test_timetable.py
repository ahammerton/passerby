import asyncio
import gzip
import json
from datetime import date, datetime

import httpx
import pytest

from app.trains.corpus import Location, Tiploc
from app.trains.services import ServiceInfo
from app.trains.stations import Station
from app.trains.timetable import (
    UK, Timetable, TimetableFeed, next_update_name, parse_schedule, run_seconds_to_ts, update_due,
)
from app.trains.tracker import TrainTracker

GLD = Station("GLD", "Guildford", 51.2372, -0.581139)
WPL = Station("WPL", "Worplesdon", 51.287885, -0.582421)
WAT = Station("WAT", "London Waterloo", 51.502838, -0.112801)
STATIONS = {s.crs: s for s in (GLD, WPL, WAT)}
TIPLOCS = {
    "GUILDFD": Tiploc("GUILDFD", "87052", "Guildford", "GLD"),
    "WRPLSDN": Tiploc("WRPLSDN", "87045", "Worplesdon", "WPL"),
    "WATRLMN": Tiploc("WATRLMN", "87219", "Waterloo Main", "WAT"),
    "WOKINGJ": Tiploc("WOKINGJ", "86032", "Woking Junction", None),
    "PORTSMH": Tiploc("PORTSMH", "88888", "Portsmouth Harbour", None),
}
CORPUS = {"87052": Location("87052", "Guildford", "GLD"), "87045": Location("87045", "Worplesdon", "WPL"),
          "87219": Location("87219", "Waterloo Main", "WAT")}
AREA = {"GUILDFD", "WRPLSDN"}
THU = date(2026, 10, 15)


def loc(kind, tiploc, arr=None, dep=None, pas=None, pub_arr=None, pub_dep=None, platform=None):
    return {"location_type": kind, "record_identity": kind, "tiploc_code": tiploc, "arrival": arr,
            "departure": dep, "pass": pas, "public_arrival": pub_arr, "public_departure": pub_dep, "platform": platform}


def schedule(uid="C12345", stp="P", start="2026-10-01", end="2026-12-12", days="1111100", headcode="1P47",
             locations=None, tx="Create", atoc="SW", category="XX", power="EMU"):
    record = {"CIF_train_uid": uid, "CIF_stp_indicator": stp, "schedule_start_date": start,
              "schedule_end_date": end, "schedule_days_runs": days, "atoc_code": atoc, "transaction_type": tx,
              "schedule_segment": {"signalling_id": headcode, "CIF_power_type": power, "CIF_timing_load": None,
                                   "CIF_speed": "100", "CIF_train_class": "B", "CIF_catering_code": "T",
                                   "CIF_sleepers": None, "CIF_train_category": category,
                                   "schedule_location": locations or []}}
    return json.dumps({"JsonScheduleV1": record}, separators=(",", ":"))


WATERLOO_PORTSMOUTH = [
    loc("LO", "WATRLMN", dep="1530", pub_dep="1530", platform="5"),
    loc("LI", "WOKINGJ", pas="1554H"),
    loc("LI", "WRPLSDN", arr="1600", dep="1601", pub_arr="1600", pub_dep="1601"),
    loc("LI", "GUILDFD", arr="1604", dep="1605", pub_arr="1604", pub_dep="1605", platform="4"),
    loc("LT", "PORTSMH", arr="1700", pub_arr="1700"),
]


def header(seq, generated, kind="full"):
    return json.dumps({"JsonTimetableV1": {"timestamp": generated, "Metadata": {"type": kind, "sequence": seq}}},
                      separators=(",", ":"))


def uk(hh_mm, d=THU):
    return datetime.fromisoformat(f"{d.isoformat()}T{hh_mm}:00").replace(tzinfo=UK).timestamp()


def build(lines):
    tt = Timetable(AREA)
    tt.apply([header(5260, uk("01:11")), *lines], THU)
    return tt


# ---------- parsing ----------

def test_parse_schedule_times_and_midnight():
    s = parse_schedule(json.loads(schedule(locations=[
        loc("LO", "WATRLMN", dep="2350", pub_dep="2350"),
        loc("LI", "GUILDFD", arr="0030H", dep="0031", pub_arr="0030", pub_dep="0000"),
        loc("LT", "PORTSMH", arr="0120", pub_arr="0120"),
    ]))["JsonScheduleV1"])
    origin, gld, end = s.calls
    assert origin.departure == 23 * 3600 + 50 * 60
    # After midnight, times continue into the next day rather than wrapping round.
    assert gld.arrival == 86400 + 30 * 60 + 30 and end.arrival == 86400 + 80 * 60
    # "0000" means "not advertised".
    assert gld.public_departure is None and gld.public


def test_runs_on_days_and_details():
    s = parse_schedule(json.loads(schedule(locations=WATERLOO_PORTSMOUTH))["JsonScheduleV1"])
    assert s.covers(THU) and not s.covers(date(2026, 10, 17))  # not on Saturdays
    assert s.details() == {"operator": "South Western Railway", "power": "Electric unit", "planned_class": None,
                           "trailing_load_t": None, "max_speed_mph": 100, "first_class": True,
                           "catering": "Trolley", "sleeper": False}


@pytest.mark.parametrize("power, load, atoc, planned_class, trailing", [
    ("DMU", "T", "GW", "165/166", None),   # GWR Reading–Gatwick
    ("DMU", "X", "SW", "159", None),       # SWR Waterloo–Exeter
    ("DMU", "V", "XC", "220/221", None),   # CrossCountry Voyager
    ("EMU", "444", "SW", "444", None),     # numeric timing load = class
    ("EMU", None, "SW", None, None),       # SWR leaves electric units blank
    ("EMU", "T", "SW", None, None),        # letter codes are diesel-unit codes only
    ("D", "1600", "ZZ", None, 1600),       # freight: trailing load, not a class
    ("DMU", "Q", "GW", None, None),        # unknown letter
])
def test_planned_class_and_trailing_load(power, load, atoc, planned_class, trailing):
    s = parse_schedule(json.loads(schedule(locations=WATERLOO_PORTSMOUTH, power=power, atoc=atoc))["JsonScheduleV1"])
    s.timing_load = load
    d = s.details()
    assert (d["planned_class"], d["trailing_load_t"]) == (planned_class, trailing)


# ---------- building and updates ----------

def test_apply_filters_to_area_and_keeps_cancellations():
    tt = build([
        schedule(locations=WATERLOO_PORTSMOUTH),
        schedule(uid="C99999", locations=[loc("LO", "PORTSMH", dep="1000"), loc("LT", "WATRLMN", arr="1130")]),
        # Too far ahead to matter yet.
        schedule(uid="C55555", start="2026-12-01", locations=WATERLOO_PORTSMOUTH),
        # A cancellation for one day: no stops, kept because the train is ours.
        schedule(stp="C", start="2026-10-16", end="2026-10-16", days="1111111"),
        schedule(uid="C99999", stp="C", start="2026-10-16", end="2026-10-16"),
    ])
    assert set(tt.by_uid) == {"C12345"}
    assert tt.sequence == 5260
    assert tt.schedule_for("C12345", THU) == (tt.schedules[("C12345", "2026-10-01", "P")], False)
    s, cancelled = tt.schedule_for("C12345", date(2026, 10, 16))
    assert cancelled and s.stp == "P"


def test_overlay_takes_precedence_and_delete_removes():
    tt = build([
        schedule(locations=WATERLOO_PORTSMOUTH),
        schedule(stp="O", start="2026-10-15", end="2026-10-15", days="1111111", headcode="1P99",
                 locations=WATERLOO_PORTSMOUTH),
    ])
    assert tt.schedule_for("C12345", THU)[0].headcode == "1P99"
    tt.apply([header(5261, uk("01:30"), "update"),
              schedule(stp="O", start="2026-10-15", tx="Delete")], THU)
    assert tt.schedule_for("C12345", THU)[0].headcode == "1P47"
    assert tt.sequence == 5261


def test_match_by_headcode_location_and_time():
    tt = build([schedule(locations=WATERLOO_PORTSMOUTH)])
    found = tt.match("1P47", {"GUILDFD"}, uk("16:08"), late_minutes=3)
    assert found and found[0].uid == "C12345" and found[1] == THU
    assert tt.match("1P47", {"GUILDFD"}, uk("18:00"), late_minutes=0) is None
    assert tt.match("2G41", {"GUILDFD"}, uk("16:05"), late_minutes=0) is None


def test_save_and_load(tmp_path):
    tt = build([schedule(locations=WATERLOO_PORTSMOUTH)])
    tt.full_built = 123.0
    tt.save(tmp_path / "tt.json.gz")
    again = Timetable.load(tmp_path / "tt.json.gz", AREA)
    assert again.schedules == tt.schedules and again.sequence == 5260 and again.full_built == 123.0
    # A different area (home or radius changed) means rebuilding.
    assert Timetable.load(tmp_path / "tt.json.gz", {"OTHER"}) is None


def test_update_naming_and_timing():
    # The full file produced early Thursday is followed by the "thu" update, out early Friday.
    generated = uk("01:11")
    assert next_update_name(generated) == "thu"
    assert not update_due(generated, uk("23:00"))
    assert update_due(generated, uk("02:05", date(2026, 10, 16)))


def gz(lines):
    return gzip.compress(("\n".join(lines) + "\n").encode())


def run_feed(tmp_path, files, now_value, existing=None):
    """Run TimetableFeed.refresh against fake downloads; returns (timetable, files requested)."""
    requested = []

    def handler(request):
        day = request.url.params["day"]
        requested.append(day)
        return httpx.Response(200, content=files[day])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            feed = TimetableFeed(client, "user", "pass", tmp_path, AREA)
            feed.timetable = existing
            return await feed.refresh()

    import app.trains.timetable as mod
    real_time = mod.time.time
    mod.time.time = lambda: now_value
    try:
        return asyncio.run(scenario()), requested
    finally:
        mod.time.time = real_time


def test_feed_full_then_updates_then_gap(tmp_path):
    fri = date(2026, 10, 16)
    files = {
        "toc-full": gz([header(5260, uk("01:11")), schedule(locations=WATERLOO_PORTSMOUTH)]),
        "toc-update-thu": gz([header(5261, uk("01:30", fri), "update"),
                              schedule(stp="O", start="2026-10-16", end="2026-10-16", days="1111111",
                                       headcode="1P99", locations=WATERLOO_PORTSMOUTH)]),
    }
    # Thursday afternoon: full download; Thursday's update isn't out until Friday.
    tt, requested = run_feed(tmp_path, files, uk("15:00"))
    assert requested == ["toc-full"] and tt.sequence == 5260
    # Friday morning: only the small update is fetched and applied.
    tt, requested = run_feed(tmp_path, files, uk("03:00", fri), existing=tt)
    assert requested == ["toc-update-thu"] and tt.sequence == 5261
    assert tt.schedule_for("C12345", fri)[0].headcode == "1P99"
    # Sunday: Friday's file skips a sequence number, so updates can't be trusted; start again from full.
    files["toc-update-fri"] = gz([header(5263, uk("01:30", date(2026, 10, 17)), "update")])
    files["toc-full"] = gz([header(5263, uk("01:11", date(2026, 10, 18))), schedule(locations=WATERLOO_PORTSMOUTH)])
    tt, requested = run_feed(tmp_path, files, uk("12:00", date(2026, 10, 18)), existing=tt)
    assert requested == ["toc-update-fri", "toc-full"] and tt.sequence == 5263


# ---------- boards and live trains ----------

def make_tracker(boards=("GLD",)):
    tracker = TrainTracker(corpus=CORPUS, stations=STATIONS, home=(GLD.lat, GLD.lon), radius_km=10, watch=None,
                           alert_seconds=60, info=ServiceInfo(TIPLOCS, STATIONS), boards=list(boards))
    tracker.set_timetable(build([schedule(locations=WATERLOO_PORTSMOUTH)]))
    return tracker


def trust_ms(hh_mm):
    return str(int(datetime.fromisoformat(f"{THU.isoformat()}T{hh_mm}:00+00:00").timestamp() * 1000))


def movement(stanox, event, at, late="0", status="ON TIME", nxt="", run=""):
    return {"header": {"msg_type": "0003"}, "body": {
        "train_id": "871P47MI15", "loc_stanox": stanox, "event_type": event, "actual_timestamp": trust_ms(at),
        "next_report_stanox": nxt, "next_report_run_time": run, "variation_status": status,
        "timetable_variation": late, "train_terminated": "false"}}


def test_board_shows_departures_with_live_running():
    tracker = make_tracker()
    [board] = tracker.snapshot(now=uk("15:40"))[0]["boards"]
    [row] = board["departures"]
    assert board["crs"] == "GLD"
    assert row["time"] == uk("16:05") and row["destination"] == "Portsmouth Harbour"
    assert row["platform"] == "4" and row["operator"] == "South Western Railway" and not row["live"]

    # The train reports leaving Worplesdon 2 minutes late: the board shows it live.
    tracker.ingest([movement("87045", "DEPARTURE", "16:03", late="2", status="LATE", nxt="87052", run="3")],
                   now=uk("16:03"))
    [row] = tracker.snapshot(now=uk("16:03"))[0]["boards"][0]["departures"]
    assert row["live"] and row["late_minutes"] == 2 and row["expected"] == uk("16:07")

    # Arrived at Guildford: still on the board until it leaves.
    tracker.ingest([movement("87052", "ARRIVAL", "16:06", late="2", status="LATE")], now=uk("16:06"))
    assert tracker.snapshot(now=uk("16:06"))[0]["boards"][0]["departures"]
    tracker.ingest([movement("87052", "DEPARTURE", "16:07", late="2", status="LATE")], now=uk("16:07"))
    assert tracker.snapshot(now=uk("16:07"))[0]["boards"][0]["departures"] == []


def test_live_train_gets_timetable_details_and_calling_points():
    tracker = make_tracker()
    tracker.ingest([movement("87045", "DEPARTURE", "16:03", late="2", status="LATE", nxt="87052", run="3")],
                   now=uk("16:03"))
    [train] = tracker.snapshot(now=uk("16:04"))[0]["positions"]
    assert train["origin"] == "London Waterloo" and train["destination"] == "Portsmouth Harbour"
    assert train["operator"] == "South Western Railway" and train["power"] == "Electric unit"
    calls = {c["name"]: c for c in train["calls"]}
    assert list(calls) == ["London Waterloo", "Worplesdon", "Guildford", "Portsmouth Harbour"]
    assert calls["Worplesdon"]["passed"] and not calls["Guildford"]["passed"]
    assert calls["Guildford"]["expected"] == uk("16:07")


def test_activation_match_and_cancellation_on_board():
    tracker = make_tracker()
    tracker.ingest([{"header": {"msg_type": "0001"}, "body": {
        "train_id": "871P47MI15", "train_uid": "C12345", "origin_dep_timestamp": trust_ms("15:30")}}],
        now=uk("15:00"))
    assert tracker.activations["871P47MI15"][:2] == ("C12345", THU)
    tracker.ingest([{"header": {"msg_type": "0002"}, "body": {"train_id": "871P47MI15"}}], now=uk("15:10"))
    [row] = tracker.snapshot(now=uk("15:40"))[0]["boards"][0]["departures"]
    assert row["cancelled"]


def test_unknown_board_station():
    with pytest.raises(ValueError, match="TRAIN_BOARDS"):
        make_tracker(boards=("ZZZ",))


# ---------- trains passing home ----------

# Three stations on one straight east–west line; home sits beside it between FEL and AFS.
LINE_FEL = Station("FEL", "Feltham", 51.44, -0.41)
LINE_AFS = Station("AFS", "Ashford (Surrey)", 51.44, -0.49)
LINE_SNS = Station("SNS", "Staines", 51.44, -0.55)
LINE_HOU = Station("HOU", "Hounslow", 51.52, -0.39)  # off to the north: a different line
LINE_STATIONS = {s.crs: s for s in (LINE_FEL, LINE_AFS, LINE_SNS, LINE_HOU)}
LINE_TIPLOCS = {
    "FELTHAM": Tiploc("FELTHAM", "87160", "Feltham", "FEL"),
    "ASHFKY": Tiploc("ASHFKY", "87170", "Ashford (Surrey)", "AFS"),
    "STAINES": Tiploc("STAINES", "87180", "Staines", "SNS"),
    "HOUNSLW": Tiploc("HOUNSLW", "87190", "Hounslow", "HOU"),
}
LINE_CORPUS = {t.stanox: Location(t.stanox, t.name, t.crs) for t in LINE_TIPLOCS.values()}
# Non-stop: timed at Staines and Feltham only, straight through Ashford (Surrey).
NON_STOP = [loc("LO", "STAINES", dep="1200", pub_dep="1200"), loc("LT", "FELTHAM", arr="1214", pub_arr="1214")]
OTHER_LINE = [loc("LO", "FELTHAM", dep="1203", pub_dep="1203"), loc("LT", "HOUNSLW", arr="1212", pub_arr="1212")]


def line_tracker():
    tracker = TrainTracker(corpus=LINE_CORPUS, stations=LINE_STATIONS, home=(51.445, -0.45), radius_km=20,
                           watch=("FEL", "AFS"), alert_seconds=60,
                           info=ServiceInfo(LINE_TIPLOCS, LINE_STATIONS), boards=[])
    tt = Timetable({"FELTHAM", "ASHFKY", "STAINES"})
    tt.apply([header(5260, uk("01:11")),
              schedule(uid="G00001", headcode="1O67", atoc="SW", locations=NON_STOP),
              schedule(uid="S00002", headcode="2D55", locations=OTHER_LINE)], THU)
    tracker.set_timetable(tt)
    return tracker


def test_trains_not_timed_at_watch_stations_still_pass_home():
    tracker = line_tracker()
    [p] = tracker.snapshot(now=uk("11:30"))[0]["watch"]["passes"]
    # Home is 0.10 of the 0.14° from Staines to Feltham: 10 of the 14 minutes.
    assert p["headcode"] == "1O67" and (p["from"], p["to"]) == ("SNS", "FEL")
    assert p["at_home"] == pytest.approx(uk("12:10"), abs=5)
    assert not p["live"] and p["operator"] == "South Western Railway" and p["destination"] == "Feltham"
    # The Hounslow train never comes past home.
    assert all(x["headcode"] != "2D55" for x in tracker.snapshot(now=uk("11:30"))[0]["watch"]["passes"])


def test_live_train_on_a_stretch_through_home_alerts_once():
    tracker = line_tracker()
    tracker.ingest([{"header": {"msg_type": "0003"}, "body": {
        "train_id": "891O67MI15", "loc_stanox": "87180", "event_type": "DEPARTURE", "actual_timestamp": trust_ms("12:02"),
        "next_report_stanox": "87160", "next_report_run_time": "14", "variation_status": "LATE",
        "timetable_variation": "2", "train_terminated": "false"}}], now=uk("12:02"))
    state, alerts = tracker.snapshot(now=uk("12:11"))
    [p] = state["watch"]["passes"]
    # Live: left Staines 12:02, due Feltham 12:16, home 10/14 of the way.
    assert p["live"] and p["uid"] == "891O67MI15" and p["at_home"] == pytest.approx(uk("12:12"), abs=5)
    assert [a["headcode"] for a in alerts] == ["1O67"]
    assert tracker.snapshot(now=uk("12:11") + 20)[1] == []


def test_scheduled_passes_use_live_delay_and_never_alert():
    tracker = line_tracker()
    # Seen 5 minutes late at Staines (activation-free match by headcode, place and time); not moving yet.
    tracker.ingest([{"header": {"msg_type": "0003"}, "body": {
        "train_id": "891O67MI15", "loc_stanox": "87180", "event_type": "ARRIVAL", "actual_timestamp": trust_ms("12:04"),
        "variation_status": "LATE", "timetable_variation": "5", "train_terminated": "false"}}], now=uk("12:04"))
    state, alerts = tracker.snapshot(now=uk("12:14"))
    [p] = state["watch"]["passes"]
    assert p["live"] and p["late_minutes"] == 5 and p["at_home"] == pytest.approx(uk("12:15"), abs=5)
    # Timetable-only entries never trigger alerts: they might not run.
    timetable_only = line_tracker()
    assert timetable_only.snapshot(now=uk("12:09") + 30)[1] == []


STOPPING = [loc("LO", "STAINES", dep="1300", pub_dep="1300"), loc("LI", "ASHFKY", arr="1304", dep="1305", pub_arr="1304", pub_dep="1305"),
            loc("LT", "FELTHAM", arr="1312", pub_arr="1312")]


def tracker_with_home(home, watch=("FEL", "AFS")):
    tracker = TrainTracker(corpus=LINE_CORPUS, stations=LINE_STATIONS, home=home, radius_km=20, watch=watch,
                           alert_seconds=60, info=ServiceInfo(LINE_TIPLOCS, LINE_STATIONS), boards=[])
    tt = Timetable({"FELTHAM", "ASHFKY", "STAINES"})
    tt.apply([header(5260, uk("01:11")), schedule(uid="S00003", headcode="2J41", locations=STOPPING)], THU)
    tracker.set_timetable(tt)
    return tracker


def test_any_watched_pair_works_and_stopping_trains_count_at_a_station_home():
    # Watching Ashford (Surrey)–Staines instead, home halfway along: the stopping train passes at 13:02.
    [p] = tracker_with_home((51.441, -0.52), watch=("AFS", "SNS")).snapshot(now=uk("12:30"))[0]["watch"]["passes"]
    assert (p["from"], p["to"]) == ("SNS", "AFS") and p["at_home"] == pytest.approx(uk("13:02"), abs=5)
    # Home right at Ashford (Surrey) station: the train stopping there counts, at its arrival time.
    tracker = tracker_with_home((51.4401, -0.49))
    [p] = tracker.snapshot(now=uk("12:30"))[0]["watch"]["passes"]
    assert p["at_home"] == pytest.approx(uk("13:04"), abs=5)
    assert tracker.watch_warning is None


def test_watch_warnings():
    assert tracker_with_home((51.445, -0.45)).watch_warning is None
    # Beyond Ashford (Surrey), not between the chosen stations.
    assert "beyond Ashford (Surrey)" in tracker_with_home((51.44, -0.52)).watch_warning
    # Between them but far from the line.
    assert "km from the Feltham–Ashford (Surrey) line" in tracker_with_home((51.47, -0.45)).watch_warning
