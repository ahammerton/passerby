import asyncio
import json
from pathlib import Path

import httpx

from app import rail_disruptions as rd

# Real responses from the NationalRail Disruptions API, 17 November 2026.
DATA = Path(__file__).parent / "data" / "rail"


def load(name):
    return json.loads((DATA / name).read_text())


def fake_api(calls, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path.split("/", 2)[2], dict(request.url.params), request.headers.get("x-apikey")))
        if status != 200:
            return httpx.Response(status, json={"error": "no"})
        path, crs = request.url.path, request.url.params.get("crsCode")
        if path.endswith("/incidents"):
            return httpx.Response(200, json=load(f"incidents_{crs}.json"))
        if path.endswith("/stationMessages"):
            return httpx.Response(200, json=load("messages_WAT.json") if crs == "WAT" else [{"crsCode": crs}])
        return httpx.Response(200, json=load("indicators.json"))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_html_to_text():
    assert rd.text("<p>A <a href=\"x\">fault</a> &amp; delays.</p><p>Until 12:00</p>") == "A fault & delays.\nUntil 12:00"
    assert rd.text("") is None and rd.text("<p> </p>") is None
    assert rd.first_link('see the <a href="https://www.nationalrail.co.uk/a?b=1&amp;c=2">site</a>') == \
        "https://www.nationalrail.co.uk/a?b=1&c=2"


def test_parse_incidents():
    incidents = rd.parse_incidents(load("incidents_CLJ.json"))
    brock = next(i for i in incidents.values() if "Brockenhurst" in i["summary"])
    assert brock["planned"] is False and brock["stations"] == ["CLJ"]
    assert [o["code"] for o in brock["operators"]] == ["XC", "SW"]
    assert brock["url"].startswith("https://www.nationalrail.co.uk/service-disruptions/")
    assert "<" not in brock["description"] and "fault with the signalling system" in brock["description"]
    assert brock["routes"].startswith("CrossCountry between Bournemouth")
    assert brock["start"] and brock["end"] is None


def test_parse_messages_and_indicators():
    msgs = rd.parse_messages(load("messages_WAT.json"))["WAT"]
    assert msgs["messages"][0]["severity"] == "Minor" and "<" not in msgs["messages"][0]["text"]
    assert msgs["messages"][0]["url"].startswith("https://www.nationalrail.co.uk/service-disruptions/")
    assert msgs["notice"].startswith("London Waterloo station is not open 24 hours")
    ind = rd.parse_indicators(load("indicators.json"))
    assert ind["SW"] == {"code": "SW", "name": "South Western Railway", "good": False,
                         "status": "Disruption in the Brockenhurst area"}
    assert ind["SE"]["good"] is True and ind["SE"]["status"] == "Good service"


def test_update_merges_stations():
    calls = []

    async def scenario():
        async with fake_api(calls) as client:
            r = rd.RailDisruptions(client, "key", [("CLJ", "Clapham Junction"), ("WAT", "London Waterloo")])
            await r.update()
            return r.snapshot()

    snap = asyncio.run(scenario())
    assert all(key == "key" for *_, key in calls) and len(calls) == 5  # 2 stations x 2, plus status
    brock = next(i for i in snap["incidents"] if "Brockenhurst" in i["summary"])
    assert brock["stations"] == ["CLJ", "WAT"] and brock["station_names"] == ["Clapham Junction", "London Waterloo"]
    assert len(snap["incidents"]) == 2  # the same incident at two stations is listed once
    assert snap["stations"][1]["messages"] and snap["stations"][0]["messages"] == []
    # Without the timetable, status lines for the train companies named in the incidents.
    # (Thameslink is named too, but isn't in the trimmed status sample.)
    assert {o["code"] for o in snap["operators"]} == {"SW", "XC", "SN", "GW"}
    assert snap["status"]["ok"] is True


def test_operators_from_timetable_win():
    r = rd.RailDisruptions(None, "key", [("WAT", "London Waterloo")])
    r.indicators = rd.parse_indicators(load("indicators.json"))
    r.incidents = rd.parse_incidents(load("incidents_WAT.json"))
    r.operators = {"SW"}
    assert [o["code"] for o in r.snapshot()["operators"]] == ["SW"]


def test_alerts_only_for_new_unplanned():
    r = rd.RailDisruptions(None, "key", [("CLJ", "Clapham Junction")])
    r.incidents = rd.parse_incidents(load("incidents_CLJ.json"))
    assert r.new_alerts() == []  # known at start-up: no alert
    r.incidents["NEW"] = {"id": "NEW", "planned": False}
    r.incidents["WORKS"] = {"id": "WORKS", "planned": True}
    assert [i["id"] for i in r.new_alerts()] == ["NEW"]
    assert r.new_alerts() == []


def test_rejected_key():
    async def scenario():
        async with fake_api([], status=401) as client:
            return await rd.check_key(client, "bad")
    assert asyncio.run(scenario()) == "The Rail Data Marketplace rejected the API key."
