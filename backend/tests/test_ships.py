import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import websockets

from app.ships import ais, feed
from app.ships.interesting import ShipInterest, assess
from app.ships.ports import Ports, parse_code_list
from app.ships.tracker import EXPIRE_MOVING, EXPIRE_STILL, VISIT_GAP_SECONDS, ShipDetails, ShipTracker

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


# ---------- port names ----------

CODE_LIST = """Change,Country,Location,Name,NameWoDiacritics,Subdivision,Status,Function,Date,IATA,Coordinates,Remarks
,BE,ANR,Antwerpen,Antwerpen,VAN,AI,12345---,0307,,5113N 00425E,
,GB,DAG,Dagenham,Dagenham,LND,RL,1-------,0001,,5132N 00009E,
,GB,SOU,Southampton,Southampton,HAM,AI,1234----,0001,,5054N 00124W,
,GB,HYT,Hythe,Hythe,HAM,RL,-----6--,0901,,,
,GB,GLD,Guildford,Guildford,SRY,RL,--3-----,0001,,,
X,GB,OLD,Old Port,Old Port,,RL,1-------,0001,,,
,TH,KSI,Ko Sichang,Ko Sichang,20,RL,1-------,0001,,1309N 10049E,
"""


def ports():
    return Ports(parse_code_list(CODE_LIST))


def test_code_list_keeps_ports():
    table = parse_code_list(CODE_LIST)
    assert set(table) == {"BEANR", "GBDAG", "GBSOU", "GBHYT", "THKSI"}  # not inland towns or removed codes
    assert table["GBSOU"] == ["Southampton", 50.9, -1.4]


@pytest.mark.parametrize("destination, names, round_trip", [
    ("GBSOU", ["Southampton"], False),
    ("GB SOU", ["Southampton"], False),
    ("BEANR > GBDAG", ["Antwerpen", "Dagenham"], False),
    ("BEANR>GBDAG", ["Antwerpen", "Dagenham"], False),
    ("GBSOU<>GBHYT", ["Southampton", "Hythe"], True),
    ("GB SOU <=> BE ANR", ["Southampton", "Antwerpen"], True),
    ("BEANR - GBSOU", ["Antwerpen", "Southampton"], False),
])
def test_destination_codes(destination, names, round_trip):
    stops, rt = ports().route(destination)
    assert [s["name"] for s in stops] == names and rt == round_trip


@pytest.mark.parametrize("destination", ["SOUTHAMPTON", "COWES", "GBSOU > SUNK ANCHORAGE", "GBXXX", "GBGLD", "", None])
def test_destinations_shown_as_typed(destination):
    assert ports().route(destination) is None


def test_far_away_ports_arent_matched_for_local_craft():
    thames_tug = {"lat": 51.5, "lon": 0.3, "length_m": 25, "category": "tug"}
    assert ports().describe("TH KSI", thames_tug) is None
    tanker = {"lat": 50.8, "lon": -1.3, "length_m": 250, "category": "tanker"}
    assert ports().describe("TH KSI", tanker)["stops"][0]["name"] == "Ko Sichang"


def test_snapshot_has_port_names():
    t = tracker()
    t.ports = ports()
    t.ingest(position(), now=NOW)
    t.ingest(message("ShipStaticData", Type=70, Destination="BEANR>GBSOU", Dimension={"A": 100, "B": 50}), now=NOW)
    route = t.snapshot(now=NOW)["ships"][0]["route"]
    assert route == {"stops": [{"code": "BEANR", "name": "Antwerpen", "country": "BE"},
                               {"code": "GBSOU", "name": "Southampton", "country": "GB"}], "round_trip": False}


# ---------- interesting ships ----------

@pytest.mark.parametrize("ship, level, reason", [
    ({"category": "beacon", "type": "Man overboard beacon"}, "emergency", "Man overboard beacon"),
    ({"category": "cargo", "status": "Emergency beacon active"}, "emergency", "Emergency beacon active"),
    ({"name": "QUEEN MARY 2", "category": "passenger", "length_m": 345}, "notable", "Cunard's transatlantic ocean liner"),
    ({"category": "passenger", "length_m": 290}, "notable", "Cruise ship, 290 m"),
    ({"category": "military", "type_code": 35, "length_m": 133}, "notable", "Warship, 133 m"),
    ({"category": "military", "type_code": 35, "length_m": 20}, "military", "Military"),
    ({"category": "cargo", "length_m": 400}, "notable", "Giant container or cargo ship, 400 m"),
])
def test_interesting(ship, level, reason):
    assert assess(ship) == {"level": level, "reason": reason}


@pytest.mark.parametrize("ship", [{"category": "passenger", "length_m": 87}, {"category": "military", "type_code": 55},
                                  {"category": "cargo", "length_m": 229}, {"category": "unknown"}])
def test_not_interesting(ship):
    assert assess(ship) is None


def test_ship_alerts():
    cruise = {"mmsi": 1, "interest": {"level": "notable"}, "still": False, "speed_kt": 12}
    berthed = {"mmsi": 2, "interest": {"level": "notable"}, "still": True, "speed_kt": 0}
    beacon = {"mmsi": 3, "interest": {"level": "emergency"}, "still": False, "speed_kt": 0}
    patrol = {"mmsi": 4, "interest": {"level": "military"}, "still": False, "speed_kt": 20}
    alerts = ShipInterest("all")
    assert [s["mmsi"] for s in alerts.new_alerts([cruise, berthed, beacon, patrol], NOW)] == [1, 3]
    assert alerts.new_alerts([cruise, beacon], NOW + 3600) == []  # once
    assert [s["mmsi"] for s in alerts.new_alerts([cruise], NOW + 25 * 3600)] == [1]  # a day later
    assert [s["mmsi"] for s in ShipInterest("emergencies").new_alerts([cruise, beacon], NOW)] == [3]
    assert ShipInterest("off").new_alerts([cruise, beacon], NOW) == []


# ---------- visit history ----------

def test_visits(tmp_path):
    store = ShipDetails(tmp_path / "ships.db")
    store.record_visits({1: {"distance_km": 40.0}, 2: {"distance_km": 70.0}}, now=NOW)
    store.record_visits({1: {"distance_km": 38.5}}, now=NOW + 600)  # the same visit, closer
    store.record_visits({1: {"distance_km": 45.0}}, now=NOW + 600 + VISIT_GAP_SECONDS + 1)  # back again
    summary = store.visit_summary()
    assert summary[1] == {"visits": 2, "first_seen": NOW, "previous": NOW + 600}
    assert summary[2] == {"visits": 1, "first_seen": NOW, "previous": None}
    recent = store.recent_visits()
    assert [(v["mmsi"], v["closest_km"]) for v in recent] == [(1, 45.0), (1, 38.5), (2, 70.0)]
    assert store.visit_stats(NOW - 1) == {"visits": 3, "ships": 2}
    t = tracker()
    t.history = {235000001: summary[1]}
    t.ingest(position(235000001), now=NOW)
    t.ingest(position(235000002), now=NOW)  # never seen before
    by_mmsi = {s["mmsi"]: s for s in t.snapshot(now=NOW)["ships"]}
    assert by_mmsi[235000001]["history"]["visits"] == 2 and by_mmsi[235000002]["history"] is None


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


# ---------- your own receiver (NMEA) ----------

from app.ships import nmea, receiver  # noqa: E402

# The recorded sample again, as a receiver would send it: !AIVDM sentences (encoded with pyais).
NMEA_SAMPLE = (Path(__file__).parent / "data" / "ais_solent.nmea").read_text().splitlines()
# Examples from the gpsd AIVDM guide (https://gpsd.gitlab.io/gpsd/AIVDM.html).
EVER_DIADEM = ["!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0*1C",
               "!AIVDM,2,2,1,A,88888888880,2*25"]
TYPE_1 = "!AIVDM,1,1,,B,15M67FC000G?ufbE`FepT@3n00Sa,0*5C"


def decode_all(lines, now=NOW):
    d = nmea.NmeaDecoder()
    return [m for line in lines if (m := d.feed(line, now))]


def test_nmea_matches_aisstream():
    """The same messages heard on the radio and from aisstream.io make the same ships."""
    by_radio, by_stream = tracker(show_leisure=True), tracker(show_leisure=True)
    for m in decode_all(NMEA_SAMPLE):
        by_radio.ingest(m, now=NOW, source="receiver")
    for m in SAMPLE:
        by_stream.ingest(m, now=NOW)
    radio = {s["mmsi"]: s for s in by_radio.snapshot(now=NOW)["ships"]}
    stream = {s["mmsi"]: s for s in by_stream.snapshot(now=NOW)["ships"]}
    assert radio.keys() == stream.keys()
    for mmsi, s in stream.items():
        r = radio[mmsi]
        assert r["source"] == "receiver" and s["source"] == "aisstream"
        for key in ("name", "callsign", "imo", "length_m", "beam_m", "destination", "status", "heading", "ais_class"):
            assert r[key] == s[key], (mmsi, key)
        assert r["lat"] == pytest.approx(s["lat"]) and r["lon"] == pytest.approx(s["lon"])


def test_nmea_known_messages():
    ship, = decode_all(EVER_DIADEM)
    _, _, d = ais.parse(ship, NOW)
    assert ship["MetaData"]["MMSI"] == 351759000
    assert d | {"eta": None} == {"name": "EVER DIADEM", "callsign": "3FOF8", "imo": 9134270, "type_code": 70,
                                 "destination": "NEW YORK", "draught_m": 12.2, "length_m": 295, "beam_m": 32,
                                 "eta": None}
    pos, = decode_all([TYPE_1])
    mmsi, p, _ = ais.parse(pos, NOW)
    assert mmsi == 366053209 and p["lat"] == pytest.approx(37.802118) and p["lon"] == pytest.approx(-122.341618)
    assert (p["speed_kt"], p["course"], p["heading"], p["status"]) == (0.0, 219.3, 1, "Restricted manoeuvrability")


def test_nmea_tolerates_noise():
    d = nmea.NmeaDecoder()
    assert d.feed(TYPE_1.replace("*5C", "*00"), NOW) is None and d.bad == 1  # bad checksum
    assert d.feed("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47", NOW) is None  # not AIS
    assert d.feed(EVER_DIADEM[1], NOW) is None  # a second half on its own
    assert d.feed(EVER_DIADEM[0], NOW) is None  # waiting for the rest...
    assert d.feed(EVER_DIADEM[1], NOW + 30) is None  # ...which came too late
    assert d.feed("\\s:AIS-catcher,c:1794900000*5A\\" + TYPE_1, NOW)  # with a tag block in front
    assert d.feed(TYPE_1.replace("!AIVDM", "!AIVDO").replace("*5C", "*5E"), NOW)  # own ship


@pytest.mark.parametrize("address, parsed", [
    ("udp://0.0.0.0:10110", ("udp", "0.0.0.0", 10110)),
    ("udp://:10110", ("udp", "0.0.0.0", 10110)),
    ("tcp://192.168.1.50:5012", ("tcp", "192.168.1.50", 5012)),
])
def test_receiver_address(address, parsed):
    assert receiver.parse_address(address) == parsed


@pytest.mark.parametrize("address", ["192.168.1.50:5012", "http://x:1", "udp://0.0.0.0", "tcp://:5012", "tcp://x:99999"])
def test_bad_receiver_address(address):
    with pytest.raises(ValueError):
        receiver.parse_address(address)


def test_receiver_over_udp_and_tcp():
    async def scenario():
        got = {"udp": [], "tcp": []}
        # UDP: Passerby listens and the receiver sends datagrams to it.
        udp_status = {}
        sock_port = _free_port()
        udp = receiver.AisReceiver(f"udp://127.0.0.1:{sock_port}", got["udp"].append, udp_status)
        udp_task = asyncio.create_task(udp.run())
        await _until(lambda: udp_status.get("connected"))
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=("127.0.0.1", sock_port))
        transport.sendto(("\r\n".join(EVER_DIADEM) + "\r\n").encode())  # both halves in one datagram
        transport.sendto(TYPE_1.encode())
        await _until(lambda: len(got["udp"]) == 2)
        transport.close()
        udp_task.cancel()

        # TCP: Passerby connects to the receiver, which serves lines.
        async def serve(reader, writer):
            writer.write(("\r\n".join(NMEA_SAMPLE) + "\r\n").encode())
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        tcp_status = {}
        port = server.sockets[0].getsockname()[1]
        tcp_task = asyncio.create_task(
            receiver.AisReceiver(f"tcp://127.0.0.1:{port}", got["tcp"].append, tcp_status).run())
        await _until(lambda: tcp_status.get("connected") is False and tcp_status.get("messages"))
        tcp_task.cancel()
        server.close()
        return got, udp_status, tcp_status

    got, udp_status, tcp_status = asyncio.run(scenario())
    assert [m["MetaData"]["MMSI"] for m in got["udp"]] == [351759000, 366053209]
    assert udp_status["ok"] and udp_status["messages"] == 2 and udp_status["address"].startswith("udp://")
    assert len(got["tcp"]) == len(SAMPLE) and tcp_status["messages"] == len(SAMPLE)
    assert "closed" in tcp_status["error"]


def test_feed_status():
    t = tracker()
    t.receiver_status = {"ok": True, "error": None, "messages": 5}
    t.status.update(ok=False, error="aisstream.io rejected the API key")
    status = t.snapshot(now=NOW)["status"]
    assert status["ok"] is True and status["error"] == "aisstream.io rejected the API key"
    assert set(status["feeds"]) == {"aisstream", "receiver"}
    only_receiver = tracker(aisstream=False)
    only_receiver.receiver_status = {"ok": None, "error": None}
    assert set(only_receiver.snapshot(now=NOW)["status"]["feeds"]) == {"receiver"}


def _free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _until(condition, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")
