import asyncio
from datetime import datetime

import httpx

from app import main, radar
from app.bus_disruptions import Disruptions, local, parse_siri_sx, when
from app.buses import BusTracker
from tests.test_buses import NOW, activity, siri
from tests.test_settings import client, env  # noqa: F401  (fixtures)

HOME = (50.853, -1.178)  # Fareham


def ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def situation(number, summary, start, end=None, stops=(), lines=(), reason="<MiscellaneousReason>roadClosed</MiscellaneousReason>",
              progress="open", link=None):
    period = f"<StartTime>{start}</StartTime>" + (f"<EndTime>{end}</EndTime>" if end else "")
    stop_xml = "".join(f"""<AffectedStopPoint><StopPointRef>{ref}</StopPointRef><StopPointName>{name}</StopPointName>
        <Location><Longitude>{lon}</Longitude><Latitude>{lat}</Latitude></Location></AffectedStopPoint>"""
                       for ref, name, lat, lon in stops)
    line_xml = "".join(f"""<AffectedLine><AffectedOperator><OperatorRef>{op}</OperatorRef>
        <OperatorName>Op {op}</OperatorName></AffectedOperator><LineRef>{line}</LineRef>
        <PublishedLineName>{line}</PublishedLineName></AffectedLine>""" for op, line in lines)
    return f"""<PtSituationElement><SituationNumber>{number}</SituationNumber><Progress>{progress}</Progress>
      <ValidityPeriod>{period}</ValidityPeriod>{reason}<Planned>true</Planned>
      <Summary>{summary}</Summary><Description>Details of {summary}</Description>
      {f"<InfoLinks><InfoLink><Uri>{link}</Uri></InfoLink></InfoLinks>" if link else ""}
      <Consequences><Consequence><Severity>slight</Severity><Affects>
        <Networks><AffectedNetwork>{line_xml}</AffectedNetwork></Networks>
        {f"<StopPoints>{stop_xml}</StopPoints>" if stops else ""}
      </Affects></Consequence></Consequences></PtSituationElement>"""


def sx(*situations):
    return f"""<?xml version="1.0"?><Siri xmlns="http://www.siri.org.uk/siri"><ServiceDelivery>
      <SituationExchangeDelivery><Situations>{"".join(situations)}</Situations></SituationExchangeDelivery>
      </ServiceDelivery></Siri>""".encode()


FEED = sx(
    situation("near", "Road Closure - Fareham Park Road", "2026-10-26T00:00:00Z", "2026-11-05T00:00:00Z",
              stops=[("S1", "Park_Road", 50.86, -1.19), ("S2", "Far end", 51.5, -0.1)], lines=[("FHAM", "F1")],
              link="https://example.org/f1"),
    situation("andover", "Stop suspension, Andover", "2026-10-01T00:00:00Z",
              stops=[("S3", "Andover", 51.21, -1.48)], lines=[("SCSO", "3")]),
    situation("routeonly", "Event on route 5", "2026-10-26T00:00:00Z", lines=[("FHAM", "5")],
              reason="<EquipmentReason>undefinedProblem</EquipmentReason>", link="http://insecure.example"),
    situation("otherroute", "Event on route 99", "2026-10-26T00:00:00Z", lines=[("FHAM", "99")]),
    situation("alton", "Alton market", "2026-10-26T00:00:00Z", lines=[("SCSO", "3"), ("SCSO", "64"), ("SCSO", "65")]),
    situation("soon", "Remembrance parade", "2026-10-30T10:00:00Z", "2026-10-30T12:00:00Z",
              stops=[("S4", "Fareham", 50.852, -1.177)], lines=[("FHAM", "28")]),
    situation("later", "Works next month", "2026-12-01T00:00:00Z", stops=[("S5", "Fareham", 50.852, -1.177)]),
    situation("over", "Finished works", "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z",
              stops=[("S6", "Fareham", 50.852, -1.177)]),
    situation("closed", "Closed one", "2026-10-26T00:00:00Z", stops=[("S7", "Fareham", 50.852, -1.177)],
              progress="closed"),
)
NOW_SX = ts("2026-10-26T09:00:00+00:00")


def test_parse():
    found = {d["id"]: d for d in parse_siri_sx(FEED)}
    assert "closed" not in found
    d = found["near"]
    assert d["summary"] == "Road Closure - Fareham Park Road" and d["reason"] == "road closed"
    assert d["stops"][0] == {"name": "Park Road", "lat": 50.86, "lon": -1.19}
    assert d["lines"] == [{"operator": "FHAM", "operator_name": "Op FHAM", "line": "F1"}]
    assert d["link"] == "https://example.org/f1" and d["planned"] and d["severity"] == "slight"
    assert found["routeonly"]["reason"] is None and found["routeonly"]["link"] is None  # http links dropped


def test_only_local_and_current():
    items = local(parse_siri_sx(FEED), *HOME, 10, {("FHAM", "5"), ("SCSO", "3")}, NOW_SX)
    by_id = {d["id"]: d for d in items}
    # Andover's names its stops, none near: a route "3" running here doesn't make it local. Nor
    # does it make Alton's local, which names no stops but mostly routes that don't run here.
    assert set(by_id) == {"near", "routeonly", "soon"}
    assert [s["name"] for s in by_id["near"]["stops"]] == ["Park Road"]  # only the nearby stops
    assert by_id["near"]["active"] and by_id["near"]["ends"] == ts("2026-11-05T00:00:00+00:00")
    assert not by_id["soon"]["active"] and by_id["soon"]["starts"] == ts("2026-10-30T10:00:00+00:00")
    assert items[-1]["id"] == "soon"  # on now first


def test_when():
    d = {"periods": [{"start": 10, "end": 20}, {"start": 30, "end": None}]}
    assert when(d, 15) == (True, None, 20)
    assert when(d, 25) == (False, 30, None)
    assert when(d, 35) == (True, None, None)
    assert when({"periods": []}, 5) == (True, None, None)


def test_buses_get_their_disruptions(tmp_path):
    def handler(request):
        if request.url.path.endswith("/siri-sx/"):
            assert request.url.params["api_key"] == "k"
            return httpx.Response(200, content=FEED)
        if request.url.path.endswith("/datafeed/"):
            return httpx.Response(200, content=siri(activity("1", 50.86, -1.19, operator="FHAM", line="F1"),
                                                    activity("2", 50.86, -1.19, operator="FHAM", line="28")))
        return httpx.Response(200, json={"results": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            t = BusTracker(c, "k", *HOME, 10, 15, tmp_path)
            t.disruptions = Disruptions(c, "k", *HOME, 10)
            await t.disruptions.fetch()
            await t.poll_once(now=NOW)
            return t
    t = asyncio.run(run())
    snap = t.snapshot()
    buses = {b["vehicle"]: b for b in snap["buses"]}
    assert buses["1"]["disruptions"] == ["near"]  # on now, on its route
    assert buses["2"]["disruptions"] == []  # the 28's parade hasn't started
    assert {d["id"] for d in snap["disruptions"]} == {"near", "soon"}  # "routeonly": route 5 isn't running
    assert snap["status"]["disruptions"]["ok"]


def test_radar_frames_checked():
    good = {"time": 1793364000, "path": "/v2/radar/9a0531cf482c"}
    frames = radar.parse({"host": radar.HOST, "radar": {"past": [
        good, {"time": 1793363400, "path": "/v2/radar/../../evil"}, {"time": "x", "path": "/v2/radar/abcdef"}]}})
    assert frames == [{"time": 1793364000, "url": f"{radar.HOST}/v2/radar/9a0531cf482c/256/{{z}}/{{x}}/{{y}}/2/1_1.png"}]
    assert radar.parse({"host": "https://evil.example", "radar": {"past": [good]}}) == []


def test_radar_endpoint(client, monkeypatch):
    async def frames(_client):
        return [{"time": 1, "url": "u"}]

    monkeypatch.setattr(radar, "frames", frames)
    assert client.get("/api/radar").json() == {"frames": [{"time": 1, "url": "u"}], "max_zoom": 7}

    async def down(_client):
        raise httpx.ConnectError("no")

    monkeypatch.setattr(radar, "frames", down)
    assert client.get("/api/radar").status_code == 502
