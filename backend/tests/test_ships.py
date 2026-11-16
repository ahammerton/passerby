import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import websockets

from app.ships import ais, feed
from app.ships.tracker import EXPIRE_MOVING, EXPIRE_STILL, ShipDetails, ShipTracker

# Real messages from aisstream.io, recorded in Southampton Water and the Solent.
SAMPLE = [json.loads(line) for line in (Path(__file__).parent / "data" / "ais_solent.jsonl").open()]
SOLENT = (50.85, -1.3)
NOW = datetime(2026, 11, 16, 16, 30, tzinfo=timezone.utc).timestamp()


def message(kind, mmsi=235000001, name="TEST SHIP", lat=50.85, lon=-1.3, when="2026-11-16 16:20:00", **body):
    return {"MessageType": kind,
            "MetaData": {"MMSI": mmsi, "MMSI_String": mmsi, "ShipName": name, "latitude": lat, "longitude": lon,
                         "time_utc": f"{when}.123456789 +0000 UTC"},
            "Message": {kind: {"UserID": mmsi, "Valid": True, **body}}}


def position(mmsi=235000001, lat=50.85, lon=-1.3, sog=12.0, status=0, when="2026-11-16 16:20:00", name="", **kw):
    return message("PositionReport", mmsi, name, lat=lat, lon=lon, when=when, Latitude=lat, Longitude=lon, Sog=sog,
                   Cog=kw.get("cog", 90.0), TrueHeading=kw.get("heading", 91), NavigationalStatus=status)


def tracker(**kw):
    return ShipTracker(*SOLENT, kw.pop("radius_km", 50), **kw)


# ---------- reading messages ----------

def test_the_recorded_sample_parses():
    t = tracker(show_leisure=True)
    for m in SAMPLE:
        t.ingest(m, now=NOW)
    ships = {s["name"]: s for s in t.snapshot(now=NOW)["ships"]}
    aida = ships["AIDANOVA"]
    assert aida["category"] == "passenger" and aida["type"] == "Passenger ship"
    assert aida["length_m"] == 337 and aida["beam_m"] == 58 and aida["flag"] == "IT"
    assert aida["destination"] == "GBSOU" and aida["ais_class"] == "A"
    assert ships["RED JET 7"]["category"] == "highspeed"
    assert ships["PHENIX"]["type"] == "Tug"
    # A small boat whose details came in two parts.
    broda = ships["BRODA"]
    assert broda["ais_class"] == "B" and broda["category"] == "pleasure" and broda["callsign"] == "2ANI6"
    assert broda["length_m"] == 10


def test_unknown_values_are_none():
    _, p, _ = ais.parse(position(sog=102.3, cog=360, heading=511, status=15), NOW)
    assert (p["speed_kt"], p["course"], p["heading"], p["status"]) == (None, None, None, None)
    assert ais.parse(position(lat=91, lon=181), NOW) is None  # nothing usable


def test_details():
    m = message("ShipStaticData", Name="EVER ECO@@@@  ", CallSign="9V3  ", ImoNumber=9811000, Type=71,
                Destination="BEANR > GBFXT       ", MaximumStaticDraught=14.2,
                Dimension={"A": 300, "B": 69, "C": 30, "D": 21},
                Eta={"Month": 11, "Day": 18, "Hour": 6, "Minute": 30})
    _, _, d = ais.parse(m, NOW)
    assert d == {"name": "EVER ECO", "callsign": "9V3", "imo": 9811000, "type_code": 71,
                 "destination": "BEANR > GBFXT", "draught_m": 14.2, "length_m": 369, "beam_m": 51,
                 "eta": datetime(2026, 11, 18, 6, 30, tzinfo=timezone.utc).timestamp()}


def test_bad_sizes_and_missing_details_are_dropped():
    # A Thames river bus that claims to be 512 m long.
    m = message("ShipStaticData", Name="HURRICANE CLIPPER", Type=0, ImoNumber=0, Destination="", CallSign="",
                Dimension={"A": 500, "B": 12, "C": 4, "D": 4}, Eta={"Month": 0, "Day": 0, "Hour": 24, "Minute": 60})
    _, _, d = ais.parse(m, NOW)
    assert d == {"name": "HURRICANE CLIPPER", "beam_m": 8}


def test_eta_without_a_year():
    jan = {"Month": 1, "Day": 4, "Hour": 9, "Minute": 0}
    assert ais.eta(jan, NOW) == datetime(2027, 1, 4, 9, tzinfo=timezone.utc).timestamp()
    nov = {"Month": 11, "Day": 10, "Hour": 9, "Minute": 0}  # a few days ago: still this year
    assert ais.eta(nov, NOW) == datetime(2026, 11, 10, 9, tzinfo=timezone.utc).timestamp()
    assert ais.eta({"Month": 2, "Day": 30, "Hour": 0, "Minute": 0}, NOW) is None


def test_types_flags_and_beacons():
    assert ais.ship_type(52) == ("tug", "Tug")
    assert ais.ship_type(84) == ("tanker", "Tanker")
    assert ais.ship_type(0) == ("unknown", None)
    assert ais.flag(232015629) == "GB" and ais.flag(538010133) == "MH" and ais.flag(982350001) == "GB"
    assert ais.flag(970123456) is None
    assert ais.is_vessel(970123456) and not ais.is_vessel(2320123) and not ais.is_vessel(992351234)  # buoy
    assert ais.beacon(972000001) == "Man overboard beacon"


def test_report_time():
    assert ais.reported_at({"time_utc": "2026-11-16 16:08:02.663927083 +0000 UTC"}, NOW) == \
        datetime(2026, 11, 16, 16, 8, 2, tzinfo=timezone.utc).timestamp()
    assert ais.reported_at({}, NOW) == NOW


# ---------- tracking ----------

def test_out_of_range_ships_are_dropped():
    t = tracker(radius_km=10)
    t.ingest(position(lat=50.85, lon=-1.3), now=NOW)
    assert len(t.positions) == 1
    t.ingest(position(lat=51.2, lon=-1.3), now=NOW)  # 39 km north: in the box's corner, out of range
    assert t.positions == {}


def test_late_reports_dont_move_a_ship_back():
    t = tracker()
    t.ingest(position(lat=50.85, when="2026-11-16 16:20:00"), now=NOW)
    t.ingest(position(lat=50.80, when="2026-11-16 16:10:00"), now=NOW)
    assert t.positions[235000001]["lat"] == 50.85


def test_trail():
    t = tracker()
    for i in range(30):
        t.ingest(position(lat=50.8 + i * 0.001, when=f"2026-11-16 16:{i:02d}:00"), now=NOW)
    t.ingest(position(lat=50.8 + 29 * 0.001 + 0.0001, when="2026-11-16 16:29:30"), now=NOW)  # 11 m: no new point
    assert len(t.trails[235000001]) == 20


def test_moored_ships_stay_longer():
    t = tracker()
    t.ingest(position(235000001, status=0), now=NOW)
    t.ingest(position(235000002, status=5, sog=0), now=NOW)
    reported = t.positions[235000001]["reported"]
    t.expire(now=reported + EXPIRE_MOVING + 1)
    assert list(t.positions) == [235000002]
    t.expire(now=reported + EXPIRE_STILL + 1)
    assert t.positions == {}


def test_leisure_hidden_unless_asked():
    t = tracker()
    t.ingest(position(235000001), now=NOW)
    t.ingest(message("ShipStaticData", 235000001, Type=37), now=NOW)  # a pleasure craft
    t.ingest(message("StandardClassBPositionReport", 235000002, Latitude=50.85, Longitude=-1.3, Sog=3,
                     Cog=10, TrueHeading=511), now=NOW)  # a small boat with no type
    t.ingest(position(235000003), now=NOW)  # a ship with no details yet
    assert [s["mmsi"] for s in t.snapshot(now=NOW)["ships"]] == [235000003]
    t.show_leisure = True
    assert {s["category"] for s in t.snapshot(now=NOW)["ships"]} == {"pleasure", "small", "unknown"}


def test_details_saved_and_loaded(tmp_path):
    t = tracker()
    t.ingest(message("ShipStaticData", 235000001, Name="ST CLARE", Type=60, Destination="GBPME",
                     Dimension={"A": 43, "B": 44, "C": 9, "D": 10}), now=NOW)
    store = ShipDetails(tmp_path / "ships.db")
    store.save(t.take_dirty())
    assert t.take_dirty() == {}
    t.ingest(message("ShipStaticData", 235000001, Name="ST CLARE", Type=60, Destination="GBPME",
                     Dimension={"A": 43, "B": 44, "C": 9, "D": 10}), now=NOW)
    assert t.take_dirty() == {}  # nothing new
    store.close()
    loaded = ShipDetails(tmp_path / "ships.db").load()
    assert loaded[235000001] == {"name": "ST CLARE", "type_code": 60, "destination": "GBPME",
                                 "length_m": 87, "beam_m": 19}
    t2 = tracker(details=loaded)
    t2.ingest(position(235000001), now=NOW)
    assert t2.snapshot(now=NOW)["ships"][0]["name"] == "ST CLARE"


# ---------- the connection ----------

def test_subscription():
    request = feed.subscription("key", 50.85, -1.3, 50)
    (south_west, north_east), = request["BoundingBoxes"]
    assert south_west[0] < 50.85 < north_east[0] and south_west[1] < -1.3 < north_east[1]  # [lat, lon]
    assert request["APIKey"] == "key" and "PositionReport" in request["FilterMessageTypes"]


async def fake_aisstream(behaviour, received):
    async def handler(ws):
        received.append(json.loads(await ws.recv()))
        await behaviour(ws)
    return await websockets.serve(handler, "127.0.0.1", 0)


def run_against(behaviour, monkeypatch, coro_factory):
    received = []

    async def scenario():
        server = await fake_aisstream(behaviour, received)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(feed, "URL", f"ws://127.0.0.1:{port}")
        try:
            return await coro_factory()
        finally:
            server.close()
            await server.wait_closed()
    return asyncio.run(scenario()), received


def test_key_check(monkeypatch):
    async def accept(ws):
        await ws.send(json.dumps({"MessageType": "SubscriptionConfirmation", "Message": {}}))

    async def reject(ws):
        await ws.close()

    assert run_against(accept, monkeypatch, lambda: feed.check_key("good"))[0] is None
    problem, received = run_against(reject, monkeypatch, lambda: feed.check_key("bad"))
    assert problem == "aisstream.io rejected that API key." and received[0]["APIKey"] == "bad"


def test_feed_delivers_messages_and_reconnects(monkeypatch):
    got, status = [], {}
    sample = SAMPLE[:3]

    async def serve(ws):
        await ws.send(json.dumps({"MessageType": "SubscriptionConfirmation", "Message": {}}))
        for m in sample:
            await ws.send(json.dumps(m).encode())  # aisstream.io sends binary frames
        await ws.close()

    async def run_until_dropped():
        task = asyncio.create_task(feed.AisStreamFeed(feed.subscription("k", *SOLENT, 50), got.append, status).run())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if status.get("connected") is False:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_against(serve, monkeypatch, run_until_dropped)
    assert got == sample
    assert status["ok"] is False and "closed" in status["error"]
