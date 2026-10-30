import asyncio
from datetime import datetime

import httpx
import pytest

from app import main, roads
from app.roads import Roads, RoadsError, check_key, local, parse
from tests.test_settings import client, env  # noqa: F401  (fixtures)

HOME = (51.2365, -0.5703)  # Guildford town centre


def ts(iso):
    return datetime.fromisoformat(iso).timestamp()


NOW = ts("2026-10-30T14:00:00+00:00")


def record(status, start, end, places, comment=None, cause=("roadMaintenance", {"roadMaintenanceType": ["maintenanceWork"]}),
           management="carriagewayClosures", group=True):
    def location(desc, road, points):
        return {
            "locLinearLocation": {
                "gmlLineString": {"locGmlLineString": {"posList": " ".join(f"{a} {b}" for a, b in points)}},
                "supplementaryPositionalDescription": {"locationDescription": desc},
            },
            "locSingleRoadLinearLocation": {"linearWithinLinearElement": [
                {"linearElement": {"locLinearElementByCode": {"roadName": road}}}]},
        }
    locs = [location(*p) for p in places]
    return {"sitRoadOrCarriagewayOrLaneManagement": {
        "roadOrCarriagewayOrLaneManagementType": {"value": management},
        "validity": {"validityStatus": status,
                     "validityTimeSpecification": {"overallStartTime": start, "overallEndTime": end}},
        "cause": {"causeType": cause[0], "detailedCauseType": cause[1]},
        "generalPublicComment": [{"comment": comment or management}],
        "locationReference": {"locLocationGroupByList": {"locationContainedInGroup": locs}} if group else locs[0],
    }}


# A3 by Guildford, straight north (four points, two of them on the line between the others).
A3 = [(51.24, -0.58), (51.245, -0.58), (51.25, -0.58), (51.26, -0.58)]
FAR = [(52.25, 0.04), (52.24, 0.05)]

PLANNED = {"D2Payload": {"situation": [
    {"idG": "1", "situationRecord": [
        record("planned", "2026-10-30T21:00:00Z", "2026-10-31T06:00:00Z",
               [("A3 northbound between A320 and A3100", "A3", A3)],
               "A3 northbound Dennis to Burpham carriageway closure",
               ("constructionWork", {"constructionWorkType": "roadImprovementOrUpgrading"})),
        record("suspended", "2026-10-31T21:00:00Z", "2026-11-01T06:00:00Z",
               [("A3 northbound between A320 and A3100", "A3", A3)], "A3 northbound Dennis to Burpham carriageway closure")]},
    {"idG": "2", "situationRecord": [
        record("active", "2025-04-01T05:00:00Z", "2026-12-21T06:00:00Z",
               [("M11 within J14", "M11", FAR), ("A3 near Guildford", "A3", [(51.255, -0.6)])],
               "Narrow lanes", management="other")]},
    {"idG": "3", "situationRecord": [  # over
        record("active", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", [("A3", "A3", A3)], "Old works")]},
    {"idG": "4", "situationRecord": [  # far off
        record("planned", "2026-10-30T21:00:00Z", "2026-10-31T06:00:00Z", [("M11", "M11", FAR)], "M11 works")]},
    {"idG": "5", "situationRecord": [  # weeks away
        record("planned", "2026-12-01T21:00:00Z", "2026-12-02T06:00:00Z", [("A3", "A3", A3)], "December works")]},
]}}

UNPLANNED = {"D2Payload": {"situation": [
    {"idG": "signs/M3-1", "situationRecord": [
        record("active", "2026-10-30T13:52:00Z", "2026-10-30T14:07:00Z", [("M3 eastbound J4 to J3", "M3", A3)],
               cause=("roadOrCarriagewayOrLaneManagement", {"roadOrCarriagewayOrLaneManagementType": {"value": "laneClosures"}}),
               management="laneClosures", group=False)]},
]}}


def test_parse():
    items = {i["id"]: i for i in parse(PLANNED, "planned")}
    a3 = items["planned:1"]
    assert a3["summary"] == "A3 northbound Dennis to Burpham carriageway closure"
    assert a3["status"] == "planned" and a3["cause"] == "road improvement or upgrading"
    assert a3["type"] == "carriageway closures" and a3["roads"] == ["A3"]
    assert a3["start"] == ts("2026-10-30T21:00:00+00:00") and a3["end"] == ts("2026-11-01T06:00:00+00:00")
    assert a3["places"] == ["A3 northbound between A320 and A3100"]
    assert items["planned:2"]["type"] is None and items["planned:2"]["status"] == "active"
    [incident] = parse(UNPLANNED, "unplanned")
    # Its only comment is its type ("laneClosures"): the place stands in as the summary.
    assert incident["summary"] == "M3 eastbound J4 to J3" and incident["comments"] == []
    assert incident["cause"] == "lane closures"


def test_local():
    items = local(parse(PLANNED, "planned") + parse(UNPLANNED, "unplanned"), *HOME, 15, NOW)
    assert [i["id"] for i in items] == ["unplanned:signs/M3-1", "planned:2", "planned:1"]
    incident, works, tonight = items
    assert incident["active"] and works["active"] and not tonight["active"]
    # Only the stretches near home are drawn, and straight runs are simplified.
    assert works["lines"] == [[[51.255, -0.6]]]
    assert tonight["lines"] == [[[51.24, -0.58], [51.26, -0.58]]]
    assert "locations" not in tonight


@pytest.mark.parametrize("status,words", [(401, "rejected"), (429, "rate limit"), (500, "answered 500")])
def test_errors(status, words):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status))) as c:
            await roads.fetch(c, "SECRET", "planned")
    with pytest.raises(RoadsError) as err:
        asyncio.run(run())
    assert words in str(err.value) and "SECRET" not in str(err.value)


def test_key_sent_as_header_and_checked():
    seen = []

    def handler(request):
        seen.append(request)
        ok = request.headers.get("Ocp-Apim-Subscription-Key") == "good"
        return httpx.Response(200, json=UNPLANNED) if ok else httpx.Response(401)

    async def run(key):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await check_key(c, key)
    assert asyncio.run(run("good")) is None
    assert "rejected" in asyncio.run(run("bad"))
    assert "good" not in str(seen[0].url) and seen[0].url.params["closureType"] == "unplanned"


def test_tracker_updates_both_lists():
    def handler(request):
        return httpx.Response(200, json=PLANNED if request.url.params["closureType"] == "planned" else UNPLANNED)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = Roads(c, "k", *HOME, 15)
            await r.update("planned", now=NOW)
            await r.update("unplanned", now=NOW)
            return r.snapshot()
    snap = asyncio.run(run())
    assert snap["status"]["ok"] and snap["status"]["count"] == 3 and len(snap["items"]) == 3


def test_roads_off_without_a_key(client):
    assert client.get("/api/config").json()["roads"] == {"enabled": False}
    assert client.get("/api/roads").status_code == 404


def test_key_checked_and_write_only(client, monkeypatch):
    async def check(_client, key):
        return "National Highways rejected the API key." if key == "bad" else None

    monkeypatch.setattr(roads, "check_key", check)
    monkeypatch.setattr(roads, "run", lambda r: asyncio.sleep(0))  # no real fetching
    assert client.put("/api/settings", json={"national_highways_api_key": "bad"}).status_code == 422
    body = client.put("/api/settings", json={"national_highways_api_key": "good", "road_radius_km": 20}).json()
    assert body["secrets"]["national_highways_api_key"] is True and "national_highways_api_key" not in body["values"]
    assert client.get("/api/config").json()["roads"] == {"enabled": True}
    assert client.get("/api/roads").json()["radius_km"] == 20
    assert client.put("/api/settings", json={"road_radius_km": 80}).status_code == 422
