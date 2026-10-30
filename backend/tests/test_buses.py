import asyncio
from datetime import datetime

import httpx
import pytest

from app import buses, main
from app.buses import BusFeedError, BusTracker, OperatorNames, bounding_box, check_bods_key, fetch_buses, parse_siri_vm
from tests.test_settings import client, env  # noqa: F401  (fixtures)

T = "2026-10-26T08:53:00+00:00"
NOW = datetime.fromisoformat(T).timestamp() + 20


def activity(vehicle, lat, lon, recorded=T, line="34", operator="SCLK", bearing="90.0", extra=""):
    return f"""<VehicleActivity>
      <RecordedAtTime>{recorded}</RecordedAtTime>
      <MonitoredVehicleJourney>
        <LineRef>{line}</LineRef><DirectionRef>outbound</DirectionRef>
        <PublishedLineName>{line}</PublishedLineName><OperatorRef>{operator}</OperatorRef>
        <OriginName>Guildford_Bus_Station</OriginName><DestinationName>Woking__Station</DestinationName>
        <VehicleLocation><Longitude>{lon}</Longitude><Latitude>{lat}</Latitude></VehicleLocation>
        {f"<Bearing>{bearing}</Bearing>" if bearing is not None else ""}
        <VehicleRef>{vehicle}</VehicleRef>{extra}
      </MonitoredVehicleJourney></VehicleActivity>"""


def siri(*activities):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <Siri xmlns="http://www.siri.org.uk/siri" version="2.0"><ServiceDelivery>
      <ResponseTimestamp>{T}</ResponseTimestamp><VehicleMonitoringDelivery>
      {"".join(activities)}
    </VehicleMonitoringDelivery></ServiceDelivery></Siri>""".encode()


def test_parse_siri_vm():
    [bus] = parse_siri_vm(siri(activity("123", 51.24, -0.57)), now=NOW)
    assert bus == {
        "id": "SCLK:123", "line": "34", "operator": "SCLK", "vehicle": "123", "direction": "outbound",
        "origin": "Guildford Bus Station", "destination": "Woking Station",
        "lat": 51.24, "lon": -0.57, "bearing": 90.0, "recorded": NOW - 20,
        "origin_ref": None, "origin_departure": None, "service_date": None,
    }


def test_stale_duplicate_and_broken_reports():
    xml = siri(
        activity("1", 51.24, -0.57, recorded="2026-10-26T08:40:00+00:00"),  # 13 min old: dropped
        activity("2", 51.20, -0.50, recorded="2026-10-26T08:52:00Z"),  # older report of vehicle 2...
        activity("2", 51.21, -0.51),  # ...replaced by the newer one
        activity("3", 91, -0.57),  # impossible position
        activity("4", "", -0.57),  # missing latitude
        activity("5", 51.24, -0.57, bearing=None),  # no bearing is fine
        activity("2", 51.25, -0.52, operator="OTHR"),  # same fleet number, other operator
    )
    found = {b["id"]: b for b in parse_siri_vm(xml, now=NOW)}
    assert set(found) == {"SCLK:2", "SCLK:5", "OTHR:2"}
    assert (found["SCLK:2"]["lat"], found["SCLK:2"]["lon"]) == (51.21, -0.51)
    assert found["SCLK:5"]["bearing"] is None


def test_bounding_box_order():
    min_lon, min_lat, max_lon, max_lat = map(float, bounding_box(51.25, -0.6, 10).split(","))
    assert min_lon < -0.6 < max_lon and min_lat < 51.25 < max_lat
    assert max_lat - min_lat == pytest.approx(20 / 111.32, rel=1e-3)
    assert max_lon - min_lon > max_lat - min_lat  # degrees of longitude are shorter here


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_errors_never_include_the_key():
    secret = "SECRETKEY123"

    def rejected(request):
        assert request.url.params["api_key"] == secret
        return httpx.Response(401, text="Unauthorized")

    def unreachable(request):
        raise httpx.ConnectError(f"failed for {request.url}")

    for handler, words in ((rejected, "rejected"), (unreachable, "couldn't reach")):
        async def run():
            async with _client(handler) as c:
                await fetch_buses(c, secret, "0,0,1,1")
        with pytest.raises(BusFeedError) as err:
            asyncio.run(run())
        assert words in str(err.value) and secret not in str(err.value)
        # Not chained to httpx's exception, whose text includes the URL (and so the key).
        assert err.value.__context__ is None or err.value.__suppress_context__


def test_check_key():
    async def run(status):
        async with _client(lambda r: httpx.Response(status, content=siri())) as c:
            return await check_bods_key(c, "k")
    assert asyncio.run(run(200)) is None
    assert "rejected" in asyncio.run(run(401))
    assert "Couldn't check" in asyncio.run(run(500))


def bods(xml, names=None, dataset_status=200):
    """A stand-in BODS: the datafeed returns `xml`; the dataset listing names operators."""
    asked = []

    def handler(request):
        if request.url.path.endswith("/datafeed/"):
            return httpx.Response(200, content=xml)
        noc = request.url.params["noc"]
        asked.append(noc)
        name = (names or {}).get(noc)
        return httpx.Response(dataset_status, json={"count": 1 if name else 0,
                                                     "results": [{"noc": noc, "operatorName": name}] if name else []})
    return handler, asked


def test_tracker_keeps_buses_inside_the_radius(tmp_path):
    xml = siri(activity("near", 51.25, -0.6), activity("corner", 51.33, -0.47))  # corner of the box
    handler, _ = bods(xml)

    async def run():
        async with _client(handler) as c:
            t = BusTracker(c, "k", 51.25, -0.6, 10, 15, tmp_path)
            await t.poll_once(now=NOW)
            return t.snapshot()
    snap = asyncio.run(run())
    assert [b["vehicle"] for b in snap["buses"]] == ["near"] and snap["status"]["ok"]
    assert (snap["buses"][0]["distance_km"], snap["buses"][0]["direction"]) == (0.0, "N")


def test_operator_names_are_looked_up_once_and_saved(tmp_path):
    xml = siri(activity("1", 51.25, -0.6, operator="SCSO"), activity("2", 51.25, -0.6, operator="SCSO"),
               activity("3", 51.25, -0.6, operator="WBSV"), activity("4", 51.25, -0.6, operator="ZZZZ"))
    handler, asked = bods(xml, {"SCSO": "Stagecoach", "WBSV": "White Bus"})

    async def run(now):
        async with _client(handler) as c:
            t = BusTracker(c, "k", 51.25, -0.6, 10, 15, tmp_path)
            await t.poll_once(now=now)
            return {b["vehicle"]: b for b in t.buses}
    buses = asyncio.run(run(NOW))
    assert sorted(asked) == ["SCSO", "WBSV", "ZZZZ"]  # one lookup per operator, not per bus
    assert (buses["1"]["operator_name"], buses["1"]["colour"]) == ("Stagecoach", "#1d4f91")
    assert buses["3"]["operator_name"] == "White Bus" and buses["3"]["colour"].startswith("#")
    assert buses["4"]["operator_name"] is None  # BODS doesn't know it

    # Saved: a restart doesn't ask again. An unknown one is retried the next day.
    asked.clear()
    asyncio.run(run(NOW + 60))
    assert asked == []
    names = OperatorNames(tmp_path / "bus_operators.json")
    assert names.name("SCSO") == "Stagecoach"
    assert names.due(["SCSO", "WBSV", "ZZZZ"], NOW + 86400 + 60) == ["ZZZZ"]
    assert names.due(["SCSO"], NOW + 31 * 86400) == ["SCSO"]  # names are refreshed monthly


def test_name_lookups_failing_dont_stop_buses(tmp_path):
    handler, _ = bods(siri(activity("1", 51.25, -0.6)), dataset_status=500)

    async def run():
        async with _client(handler) as c:
            t = BusTracker(c, "k", 51.25, -0.6, 10, 15, tmp_path)
            await t.poll_once(now=NOW)
            return t.buses
    [bus] = asyncio.run(run())
    assert bus["operator_name"] is None and bus["colour"]


def test_operator_colours(tmp_path):
    from app.buses import PALETTE, brand_colour

    assert brand_colour("Stagecoach") == brand_colour("Stagecoach South West") == "#1d4f91"
    assert brand_colour("First Bus") == "#6b2c91" and brand_colour("National Express (UK Coach)") == "#c8102e"
    assert brand_colour("Falcon Buses") is None

    names = {"SCSO": "Stagecoach", "FALC": "Falcon Buses", "WBSV": "White Bus", "SFGC": "Safeguard Coaches",
             "COMT": "Compass Travel"}
    handler, _ = bods(siri(*(activity(n, 51.25, -0.6, operator=n) for n in names)), names)

    async def run(path):
        async with _client(handler) as c:
            t = BusTracker(c, "k", 51.25, -0.6, 10, 15, path)
            for _ in range(2):  # a few operators are looked up per poll
                await t.poll_once(now=NOW)
            return {b["operator"]: b["colour"] for b in t.buses}
    colours = asyncio.run(run(tmp_path))
    assert colours["SCSO"] == "#1d4f91"
    others = [colours[n] for n in ("FALC", "WBSV", "SFGC", "COMT")]
    assert len(set(others)) == 4 and set(others) <= set(PALETTE)  # all different
    assert asyncio.run(run(tmp_path)) == colours  # and the same after a restart


def test_buses_off_without_a_key(client):
    assert client.get("/api/config").json()["buses"] == {"enabled": False, "min_zoom": 12}
    assert client.get("/api/buses").status_code == 404


def test_bods_key_checked_and_write_only(client, monkeypatch):
    async def bad(_client, key):
        return "BODS rejected the API key." if key == "bad" else None

    monkeypatch.setattr(main, "check_bods_key", bad)
    assert client.put("/api/settings", json={"bods_api_key": "bad"}).status_code == 422
    # Buses start once a good key is saved; the key itself never comes back.
    started = []
    monkeypatch.setattr(main, "poll_buses", lambda state: started.append(state.buses) or asyncio.sleep(0))
    monkeypatch.setattr(main, "keep_bus_timetable", lambda state: asyncio.sleep(0))  # no 300 MB download
    body = client.put("/api/settings", json={"bods_api_key": "good", "bus_min_zoom": 14}).json()
    assert body["secrets"]["bods_api_key"] is True and "bods_api_key" not in body["values"]
    assert client.get("/api/config").json()["buses"] == {"enabled": True, "min_zoom": 14}
    assert started and started[0].api_key == "good"
    assert client.put("/api/settings", json={"bus_min_zoom": 5}).status_code == 422


def test_bus_route_endpoint(client, monkeypatch):
    async def good(_client, key):
        return None

    monkeypatch.setattr(main, "check_bods_key", good)
    monkeypatch.setattr(main, "poll_buses", lambda state: asyncio.sleep(0))
    monkeypatch.setattr(main, "keep_bus_timetable", lambda state: asyncio.sleep(0))
    assert client.put("/api/settings", json={"bods_api_key": "good"}).status_code == 200
    tracker = main.tracker().buses
    tracker.buses = [{"id": "SCSO:1", "line": "34"}, {"id": "SCSO:2", "line": "35"}]

    class Timetable:
        def route(self, bus):
            return {"trip_id": "T1", "follows_roads": True, "line": [[51, 0], [52, 0]], "stops": []} \
                if bus["line"] == "34" else None

    assert client.get("/api/buses/route", params={"id": "SCSO:1"}).status_code == 404  # no timetable yet
    tracker.timetable = Timetable()
    assert client.get("/api/buses/route", params={"id": "SCSO:1"}).json()["trip_id"] == "T1"
    assert "matched" in client.get("/api/buses/route", params={"id": "SCSO:2"}).json()["detail"]
    assert "any more" in client.get("/api/buses/route", params={"id": "SCSO:9"}).json()["detail"]
