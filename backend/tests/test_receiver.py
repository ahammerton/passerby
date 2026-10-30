import asyncio
import time
from types import SimpleNamespace

import httpx

from app import main
from app.sources.readsb import ReadsbSource, find_receiver, parse_aircraft, parse_response, receiver_urls

# An aircraft exactly as the Flightradar24 feeder's dump1090 reported it (older field names).
FR24_ENTRY = {"hex": "4b1800", "squawk": "2004", "flight": "SWR13Y  ", "lat": 51.450138, "lon": -0.380554,
              "nucp": 7, "seen_pos": 3.5, "altitude": 2975, "vert_rate": 1664, "track": 132, "speed": 176,
              "mlat": [], "tisb": [], "messages": 152, "seen": 2.4, "rssi": -22.9}


def test_older_dump1090_field_names():
    a = parse_aircraft(FR24_ENTRY, now=1000.0)
    assert (a.callsign, a.altitude_ft, a.speed_kt, a.vertical_rate_fpm) == ("SWR13Y", 2975, 176, 1664)
    assert a.position_time == 996.5 and a.source == "receiver"
    assert parse_aircraft({"hex": "abc123", "altitude": "ground"}, now=1000.0).on_ground


def test_receiver_clock_out_by_more_than_5s_is_ignored():
    body = {"now": 5000.0, "aircraft": [FR24_ENTRY]}
    [a] = parse_response(body, "receiver", local_now=1000.0)
    assert a.position_time == 996.5  # measured against our clock, not the receiver's
    [b] = parse_response({"now": 1002.0, "aircraft": [FR24_ENTRY]}, "receiver", local_now=1000.0)
    assert b.position_time == 998.5  # close enough: the receiver's own time is kept


def test_receiver_urls():
    assert receiver_urls("192.168.1.50")[0] == "http://192.168.1.50/tar1090/data/aircraft.json"
    assert "http://192.168.1.50/dump1090/data/aircraft.json" in receiver_urls("192.168.1.50")
    assert receiver_urls("http://pi.local/data/aircraft.json") == ["http://pi.local/data/aircraft.json"]
    assert receiver_urls("") == []


def test_find_receiver_tries_common_paths():
    tried = []

    def handler(request):
        tried.append(request.url.path)
        if request.url.path == "/dump1090/data/aircraft.json":
            return httpx.Response(200, json={"now": 1, "aircraft": [FR24_ENTRY, {"hex": "abcdef"}]})
        return httpx.Response(404, text="not here")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await find_receiver(client, "192.168.1.50"), await find_receiver(client, "10.0.0.9/nothing.json")

    found, missing = asyncio.run(scenario())
    assert found == ("http://192.168.1.50/dump1090/data/aircraft.json", 2, 1)
    assert missing is None
    assert tried[:4] == ["/tar1090/data/aircraft.json", "/skyaware/data/aircraft.json",
                         "/dump1090-fa/data/aircraft.json", "/dump1090/data/aircraft.json"]


def fake_state(mode, receiver_ok, receiver_age):
    receiver = ReadsbSource(httpx.AsyncClient(), url="http://x/aircraft.json", interval=2)
    online = SimpleNamespace(name="adsblol", local=False, interval=30, priority=5)
    status = {
        "receiver": {"ok": receiver_ok, "last_update": time.time() - receiver_age, "count": 10},
        "adsblol": {"ok": True, "last_update": None, "count": 0, "error": None},
    }
    updates = []
    return SimpleNamespace(settings=SimpleNamespace(online_feeds=mode), sources=[receiver, online],
                           source_status=status, aggregator=SimpleNamespace(update=lambda n, a: updates.append((n, a)))), online, updates


def test_receiver_working():
    assert main.receiver_working(fake_state("backup", True, 3)[0])
    assert not main.receiver_working(fake_state("backup", True, 120)[0])  # stopped answering
    assert not main.receiver_working(fake_state("backup", False, 3)[0])  # erroring


def test_backup_mode_online_feeds_stand_by(monkeypatch):
    class Stop(Exception):
        pass

    async def one_round(_):
        raise Stop

    monkeypatch.setattr(main.asyncio, "sleep", one_round)
    fetched = []

    async def fetch():
        fetched.append(1)
        return []

    def run(state, online):
        online.fetch = fetch
        try:
            asyncio.run(main.poll_source(state, online))
        except Stop:
            pass

    # Receiver working, backup mode: the online feed stands by and clears its aircraft.
    state, online, updates = fake_state("backup", True, 3)
    run(state, online)
    assert fetched == [] and state.source_status["adsblol"]["standby"] and updates == [("adsblol", [])]
    # Receiver down: the online feed fetches again.
    state, online, _ = fake_state("backup", True, 120)
    run(state, online)
    assert fetched == [1]
    # Fill mode: the online feed always runs.
    state, online, _ = fake_state("fill", True, 3)
    run(state, online)
    assert fetched == [1, 1]
