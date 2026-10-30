import asyncio
import os
import time

import httpx

from app.airlines import AIRLINES, LOGO_RETRY, PNG_MAGIC, Logos, identify

PNG = PNG_MAGIC + b"logo"


def test_airline_from_callsign():
    assert identify("BAW291") == {"airline_icao": "BAW", "airline_name": "British Airways",
                                  "livery": {"body": AIRLINES["BAW"][1], "tail": AIRLINES["BAW"][2]}}
    assert identify("EZY12AB")["airline_name"] == "easyJet"


def test_unlisted_airline_still_gets_a_logo_code():
    # No colours, but the route lookup's airline name and the logo still work.
    assert identify("XLF123", route_airline="Somewhere Air") == {
        "airline_icao": "XLF", "airline_name": "Somewhere Air", "livery": None}


def test_not_airlines():
    assert identify(None) == {}
    assert identify("GABCD") == {}  # a registration used as the callsign
    assert identify("N123AB") == {}
    assert identify("RRR6512", military=True) == {}  # RAF


def test_every_colour_is_a_hex_colour():
    for name, body, tail in AIRLINES.values():
        assert name
        for c in (body, tail):
            assert len(c) == 7 and c[0] == "#" and int(c[1:], 16) >= 0


def client(answers: dict[str, int], seen: list[str]):
    def handler(request):
        seen.append(request.url.path)
        for part, status in answers.items():
            if part in request.url.path:
                return httpx.Response(status, content=PNG if status == 200 else b"")
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_logo_fetched_once_and_cached(tmp_path):
    seen = []

    async def scenario():
        logos = Logos(client({"flightaware_logos/BAW": 200}, seen), tmp_path)
        assert (await logos.get("BAW")).read_bytes() == PNG
        assert (await logos.get("BAW")).read_bytes() == PNG
    asyncio.run(scenario())
    assert len(seen) == 1


def test_logo_falls_back_to_radarbox(tmp_path):
    seen = []

    async def scenario():
        return await Logos(client({"radarbox_logos/ABC": 200}, seen), tmp_path).get("ABC")
    assert asyncio.run(scenario()).name == "ABC.png"
    assert len(seen) == 2


def test_missing_logo_remembered_then_retried(tmp_path):
    seen = []

    async def scenario():
        logos = Logos(client({}, seen), tmp_path)
        assert await logos.get("ZZZ") is None
        assert await logos.get("ZZZ") is None
        assert len(seen) == 2  # both sources, once
        old = time.time() - LOGO_RETRY - 1
        os.utime(tmp_path / "ZZZ.none", (old, old))
        assert await logos.get("ZZZ") is None
        assert len(seen) == 4
    asyncio.run(scenario())
