"""Adds aircraft details, routes and photos from free lookup services.

- adsbdb.com:       ICAO24 -> type/registration/operator, callsign -> route
- planespotters.net: ICAO24 -> photo (requires a contact in the User-Agent and
  that photos are hotlinked with photographer credit and a link back)

Every result, including "not found", is cached in SQLite so each aircraft is
looked up at most once per TTL. adsbdb lookups run through a single throttled
worker, nearest aircraft first.
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from .models import Aircraft, Route, RouteEnd

log = logging.getLogger(__name__)

ADSBDB_URL = "https://api.adsbdb.com/v0"
PLANESPOTTERS_URL = "https://api.planespotters.net/pub/photos/hex"

DAY = 86400
TTL = {"aircraft": 30 * DAY, "route": 3 * DAY, "photo": 7 * DAY}
# "Not found" answers are retried sooner in case the databases catch up.
NEGATIVE_TTL = DAY

# Airline-style callsigns (BAW291, EZY12AB). Registrations used as callsigns
# by private flights (GABCD, N123AB) have no route to look up.
AIRLINE_CALLSIGN = re.compile(r"^[A-Z]{3}[0-9][0-9A-Z]{0,3}$")
ICAO24 = re.compile(r"^[0-9a-f]{6}$")


class RateLimited(Exception):
    pass


# ---------- response parsers ----------

def parse_adsbdb_aircraft(body: dict[str, Any]) -> dict | None:
    info = (body.get("response") or {}).get("aircraft") if isinstance(body.get("response"), dict) else None
    if not info:
        return None
    name = " ".join(p for p in (info.get("manufacturer"), info.get("type")) if p) or None
    return {
        "registration": info.get("registration"),
        "type_code": info.get("icao_type"),
        "aircraft_name": name,
        "operator": info.get("registered_owner"),
    }


def _route_end(airport: dict) -> dict:
    return RouteEnd(
        iata=airport.get("iata_code"),
        icao=airport.get("icao_code"),
        name=airport.get("name"),
        city=airport.get("municipality"),
        country=airport.get("country_name"),
    ).model_dump()


def parse_adsbdb_route(body: dict[str, Any]) -> dict | None:
    route = (body.get("response") or {}).get("flightroute") if isinstance(body.get("response"), dict) else None
    if not route or not route.get("origin") or not route.get("destination"):
        return None
    return Route(
        airline=(route.get("airline") or {}).get("name"),
        origin=_route_end(route["origin"]),
        destination=_route_end(route["destination"]),
    ).model_dump()


def _https(url) -> bool:
    return isinstance(url, str) and url.lower().startswith("https://")


def parse_planespotters(body: dict[str, Any]) -> dict | None:
    photos = body.get("photos") or []
    if not photos:
        return None
    p = photos[0]
    thumbnail = (p.get("thumbnail_large") or p.get("thumbnail") or {}).get("src")
    if not _https(thumbnail):
        return None
    return {
        "thumbnail": thumbnail,
        # Only ever a web link: the page puts it straight into an <a href>.
        "link": p.get("link") if _https(p.get("link")) else None,
        "photographer": p.get("photographer"),
    }


# ---------- cache ----------

class EnrichmentCache:
    """SQLite-backed cache mirrored in memory (the data set is small)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache (kind TEXT, key TEXT, data TEXT,"
            " fetched_at REAL, PRIMARY KEY (kind, key))"
        )
        self._mem: dict[tuple[str, str], tuple[dict | None, float]] = {
            (kind, key): (json.loads(data) if data else None, fetched_at)
            for kind, key, data, fetched_at in self.db.execute("SELECT * FROM cache")
        }

    def get(self, kind: str, key: str, now: float | None = None) -> tuple[bool, dict | None]:
        """(hit, data). A hit with data None means "known not to exist"."""
        entry = self._mem.get((kind, key))
        if entry is None:
            return False, None
        data, fetched_at = entry
        ttl = TTL[kind] if data is not None else NEGATIVE_TTL
        now = time.time() if now is None else now
        if now - fetched_at > ttl:
            return False, None
        return True, data

    def put(self, kind: str, key: str, data: dict | None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._mem[(kind, key)] = (data, now)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)",
                (kind, key, json.dumps(data) if data is not None else None, now),
            )

    def close(self) -> None:
        self.db.close()


# ---------- enricher ----------

class Enricher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: EnrichmentCache,
        *,
        delay: float = 1.0,
        photos_enabled: bool = True,
    ):
        self.client = client
        self.cache = cache
        self.delay = delay
        self.photos_enabled = photos_enabled
        self.queue: asyncio.PriorityQueue[tuple[float, str, str]] = asyncio.PriorityQueue()
        self._pending: set[tuple[str, str]] = set()
        self._photo_locks: dict[str, asyncio.Lock] = {}

    def _lookup(self, kind: str, key: str, distance: float) -> dict | None:
        hit, data = self.cache.get(kind, key)
        if not hit and (kind, key) not in self._pending:
            self._pending.add((kind, key))
            self.queue.put_nowait((distance, kind, key))
        return data

    def annotate(self, snapshot: list[Aircraft]) -> list[Aircraft]:
        """Copy cached details onto each aircraft and queue lookups for the rest."""
        result = []
        for a in snapshot:
            distance = a.distance_km or 0.0
            update: dict[str, Any] = {}
            info = self._lookup("aircraft", a.icao24, distance)
            if info:
                update.update({k: v for k, v in info.items() if v and getattr(a, k) is None})
            if a.callsign and AIRLINE_CALLSIGN.match(a.callsign):
                route = self._lookup("route", a.callsign, distance)
                if route:
                    update["route"] = Route(**route)
            result.append(a.model_copy(update=update) if update else a)
        return result

    async def _fetch_adsbdb(self, kind: str, key: str) -> dict | None:
        path = f"aircraft/{key}" if kind == "aircraft" else f"callsign/{key}"
        resp = await self.client.get(f"{ADSBDB_URL}/{path}")
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            raise RateLimited()
        resp.raise_for_status()
        parse = parse_adsbdb_aircraft if kind == "aircraft" else parse_adsbdb_route
        return parse(resp.json())

    async def run(self) -> None:
        """Background worker: one adsbdb request every `delay` seconds."""
        while True:
            _, kind, key = await self.queue.get()
            try:
                self.cache.put(kind, key, await self._fetch_adsbdb(kind, key))
            except RateLimited:
                log.warning("adsbdb rate limit hit, pausing for 60s")
                await asyncio.sleep(60)
            except Exception as exc:
                log.warning("adsbdb %s lookup for %s failed: %s", kind, key, exc)
            finally:
                # Failures are simply re-queued by the next snapshot.
                self._pending.discard((kind, key))
            await asyncio.sleep(self.delay)

    async def photo(self, icao24: str) -> dict | None:
        """Photo for one aircraft, fetched on demand when a popup is opened."""
        hit, data = self.cache.get("photo", icao24)
        if hit or not self.photos_enabled:
            return data
        # Concurrent popups for the same aircraft share one request.
        async with self._photo_locks.setdefault(icao24, asyncio.Lock()):
            hit, data = self.cache.get("photo", icao24)
            if hit:
                return data
            resp = await self.client.get(f"{PLANESPOTTERS_URL}/{icao24}")
            resp.raise_for_status()
            data = parse_planespotters(resp.json())
            self.cache.put("photo", icao24, data)
            return data
