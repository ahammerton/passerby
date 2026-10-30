import asyncio
import time

import httpx

from app.enrichment import (
    NEGATIVE_TTL, EnrichmentCache, Enricher, parse_adsbdb_aircraft, parse_adsbdb_route,
    parse_planespotters,
)
from app.models import Aircraft
from app.sources.community import CommunitySource
from app.sources.readsb import parse_response

# Trimmed real responses captured from the live APIs.
ADSBDB_AIRCRAFT = {"response": {"aircraft": {
    "type": "777 236ER", "icao_type": "B772", "manufacturer": "Boeing", "mode_s": "4007F9",
    "registration": "G-YMMN", "registered_owner": "British Airways",
}}}
ADSBDB_ROUTE = {"response": {"flightroute": {
    "callsign": "BAW291", "airline": {"name": "British Airways"},
    "origin": {"iata_code": "LHR", "icao_code": "EGLL", "name": "London Heathrow Airport",
               "municipality": "London", "country_name": "United Kingdom"},
    "destination": {"iata_code": "SFO", "icao_code": "KSFO", "name": "San Francisco International Airport",
                    "municipality": "San Francisco", "country_name": "United States"},
}}}
PLANESPOTTERS = {"photos": [{
    "id": "1883785",
    "thumbnail": {"src": "https://t.plnspttrs.net/16187/1883785_t.jpg"},
    "thumbnail_large": {"src": "https://t.plnspttrs.net/16187/1883785_280.jpg"},
    "link": "https://www.planespotters.net/photo/1883785/g-ymmn",
    "photographer": "Z Song",
}]}


def test_parse_adsbdb_aircraft():
    assert parse_adsbdb_aircraft(ADSBDB_AIRCRAFT) == {
        "registration": "G-YMMN", "type_code": "B772",
        "aircraft_name": "Boeing 777 236ER", "operator": "British Airways",
    }
    assert parse_adsbdb_aircraft({"response": "unknown aircraft"}) is None


def test_parse_adsbdb_route():
    route = parse_adsbdb_route(ADSBDB_ROUTE)
    assert route["airline"] == "British Airways"
    assert route["origin"]["iata"] == "LHR" and route["destination"]["city"] == "San Francisco"
    assert parse_adsbdb_route({"response": "unknown callsign"}) is None


def test_parse_planespotters():
    photo = parse_planespotters(PLANESPOTTERS)
    assert photo == {
        "thumbnail": "https://t.plnspttrs.net/16187/1883785_280.jpg",
        "link": "https://www.planespotters.net/photo/1883785/g-ymmn",
        "photographer": "Z Song",
    }
    assert parse_planespotters({"photos": []}) is None


def test_cache_ttl_and_persistence(tmp_path):
    cache = EnrichmentCache(tmp_path / "e.db")
    cache.put("aircraft", "abc123", {"registration": "G-TEST"}, now=1000)
    cache.put("aircraft", "def456", None, now=1000)
    assert cache.get("aircraft", "abc123", now=2000) == (True, {"registration": "G-TEST"})
    assert cache.get("aircraft", "def456", now=2000) == (True, None)
    # Negative results expire sooner than positive ones.
    assert cache.get("aircraft", "def456", now=1000 + NEGATIVE_TTL + 1) == (False, None)
    assert cache.get("aircraft", "abc123", now=1000 + NEGATIVE_TTL + 1)[0]
    cache.close()
    # Survives a restart.
    assert EnrichmentCache(tmp_path / "e.db").get("aircraft", "abc123", now=2000)[0]


def mock_client(routes: dict[str, tuple[int, dict]], calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        status, body = routes.get(request.url.path, (404, {"response": "unknown"}))
        return httpx.Response(status, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def aircraft(icao, callsign=None, distance=5.0, **kw):
    return Aircraft(icao24=icao, source="opensky", last_seen=time.time(), lat=51.5, lon=-0.1,
                    callsign=callsign, distance_km=distance, **kw)


def test_enricher_queues_fetches_and_annotates(tmp_path):
    calls: list[str] = []

    async def scenario():
        client = mock_client({
            "/v0/aircraft/4007f9": (200, ADSBDB_AIRCRAFT),
            "/v0/callsign/BAW291": (200, ADSBDB_ROUTE),
        }, calls)
        enricher = Enricher(client, EnrichmentCache(tmp_path / "e.db"), delay=0)
        snapshot = [
            aircraft("4007f9", "BAW291", distance=20, type_code="B77W"),
            aircraft("aaaaaa", "GABCD", distance=3),  # private callsign: no route lookup
        ]
        first = enricher.annotate(snapshot)
        assert first[0].route is None
        # A second snapshot before the worker runs must not queue duplicates.
        enricher.annotate(snapshot)
        assert enricher.queue.qsize() == 3

        worker = asyncio.create_task(enricher.run())
        while enricher.queue.qsize() or enricher._pending:
            await asyncio.sleep(0.01)
        worker.cancel()

        a, b = enricher.annotate(snapshot)
        assert a.route.destination.iata == "SFO"
        assert a.registration == "G-YMMN" and a.operator == "British Airways"
        assert a.type_code == "B77W"  # existing values are not overwritten
        assert b.route is None and b.registration is None
        await client.aclose()

    asyncio.run(scenario())
    # Nearest aircraft is looked up first.
    assert calls[0] == "/v0/aircraft/aaaaaa"
    assert sorted(calls[1:]) == ["/v0/aircraft/4007f9", "/v0/callsign/BAW291"]


def test_photo_is_fetched_once(tmp_path):
    calls: list[str] = []

    async def scenario():
        client = mock_client({"/pub/photos/hex/4007f9": (200, PLANESPOTTERS)}, calls)
        enricher = Enricher(client, EnrichmentCache(tmp_path / "e.db"))
        results = await asyncio.gather(*(enricher.photo("4007f9") for _ in range(3)))
        assert all(r["photographer"] == "Z Song" for r in results)
        disabled = Enricher(client, EnrichmentCache(tmp_path / "e2.db"), photos_enabled=False)
        assert await disabled.photo("4007f9") is None
        await client.aclose()

    asyncio.run(scenario())
    assert calls == ["/pub/photos/hex/4007f9"]


def test_community_response_uses_millisecond_timestamps():
    body = {"now": 1_700_000_000_500, "ac": [
        {"hex": "43c6f1", "flight": "RRR123 ", "lat": 51.5, "lon": -0.1, "alt_baro": 20000,
         "seen": 0.5, "dbFlags": 1, "t": "A400"},
    ]}
    [a] = parse_response(body, "adsblol")
    assert a.source == "adsblol" and a.military and a.type_code == "A400"
    assert a.last_seen == 1_700_000_000.0


def test_community_source_url_and_error():
    calls: list[str] = []

    async def scenario():
        client = mock_client({"/v2/point/51.5/-0.1/81": (200, {"error": "contact us"})}, calls)
        src = CommunitySource(client, name="airplaneslive", base_url="https://api.example",
                              lat=51.5, lon=-0.1, radius_km=150, interval=10)
        try:
            await src.fetch()
        except RuntimeError as exc:
            return str(exc)
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) == "contact us"
    assert calls == ["/v2/point/51.5/-0.1/81"]
