"""Live bus positions from the Bus Open Data Service (BODS), England only.

https://data.bus-data.dft.gov.uk — a free account gives an API key. The SIRI-VM datafeed
returns every vehicle reporting inside a bounding box, refreshed by BODS every ~10 s.

The key goes in the URL (BODS takes it as a query parameter), so no error message built
here ever includes the URL: httpx puts it in its exception text, which would otherwise end
up in the log and in the status shown to browsers.
"""

import hashlib
import json
import logging
import math
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx

from .geo import bearing_deg, compass, haversine_km

log = logging.getLogger(__name__)

DATAFEED_URL = "https://data.bus-data.dft.gov.uk/api/v1/datafeed/"
DATASET_URL = "https://data.bus-data.dft.gov.uk/api/v1/dataset/"
NS = {"s": "http://www.siri.org.uk/siri"}
# Reports older than this are dropped: a bus that stopped reporting shouldn't sit on the map.
MAX_AGE_S = 300
# Refuse anything implausibly large rather than parse it.
MAX_BYTES = 30_000_000


class BusFeedError(Exception):
    """A failure described without the request URL (which carries the API key)."""


def bounding_box(lat: float, lon: float, radius_km: float) -> str:
    """BODS order: min longitude, min latitude, max longitude, max latitude."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return ",".join(f"{v:.5f}" for v in (lon - dlon, lat - dlat, lon + dlon, lat + dlat))


def _text(el: ET.Element | None, path: str) -> str | None:
    found = el.find(path, NS) if el is not None else None
    value = (found.text or "").strip() if found is not None else ""
    return value or None


def _place(name: str | None) -> str | None:
    # Many operators send stop names with underscores for spaces ("Guildford_Bus_Station").
    return " ".join(name.replace("_", " ").split()) if name else None


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_siri_vm(xml: bytes, now: float | None = None) -> list[dict]:
    """Vehicles from a SIRI-VM document: the latest report for each, recent ones only."""
    now = time.time() if now is None else now
    root = ET.fromstring(xml)
    latest: dict[str, dict] = {}
    for activity in root.iterfind(".//s:VehicleActivity", NS):
        journey = activity.find("s:MonitoredVehicleJourney", NS)
        if journey is None:
            continue
        try:
            lat = float(_text(journey, "s:VehicleLocation/s:Latitude") or "nan")
            lon = float(_text(journey, "s:VehicleLocation/s:Longitude") or "nan")
        except ValueError:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):  # also rejects NaN
            continue
        recorded = _timestamp(_text(activity, "s:RecordedAtTime"))
        if recorded is None or now - recorded > MAX_AGE_S:
            continue
        operator = _text(journey, "s:OperatorRef")
        vehicle = _text(journey, "s:VehicleRef")
        if not vehicle:
            continue
        key = f"{operator or '?'}:{vehicle}"
        if key in latest and latest[key]["recorded"] >= recorded:
            continue
        bearing = _text(journey, "s:Bearing")
        try:
            bearing = float(bearing) % 360 if bearing is not None else None
        except ValueError:
            bearing = None
        latest[key] = {
            "id": key,
            "line": _text(journey, "s:PublishedLineName") or _text(journey, "s:LineRef"),
            "operator": operator,
            "vehicle": vehicle,
            "direction": _text(journey, "s:DirectionRef"),
            "origin": _place(_text(journey, "s:OriginName")),
            "destination": _place(_text(journey, "s:DestinationName")),
            "lat": lat,
            "lon": lon,
            "bearing": bearing,
            "recorded": recorded,
            # For matching to the timetable: the journey's first stop, when it was due to leave
            # it, and the day it runs (a journey after midnight belongs to the day before).
            "origin_ref": _text(journey, "s:OriginRef"),
            "origin_departure": _timestamp(_text(journey, "s:OriginAimedDepartureTime")),
            "service_date": _text(journey, "s:FramedVehicleJourneyRef/s:DataFrameRef"),
        }
    return list(latest.values())


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    """GET from BODS, with every failure described without the URL (it carries the key)."""
    try:
        resp = await client.get(url, params=params, timeout=30)
    except httpx.TimeoutException:
        raise BusFeedError("BODS didn't answer in time") from None
    except httpx.HTTPError as exc:
        raise BusFeedError(f"couldn't reach BODS ({type(exc).__name__})") from None
    if resp.status_code in (401, 403):
        raise BusFeedError("BODS rejected the API key")
    if resp.status_code == 429:
        raise BusFeedError("BODS rate limit reached")
    if resp.status_code != 200:
        raise BusFeedError(f"BODS answered {resp.status_code}")
    if len(resp.content) > MAX_BYTES:
        raise BusFeedError("BODS sent an unexpectedly large response")
    return resp


async def fetch_buses(client: httpx.AsyncClient, api_key: str, box: str) -> bytes:
    return (await _get(client, DATAFEED_URL, {"api_key": api_key, "boundingBox": box})).content


# ---------- operators: names from BODS, colours from their brands ----------

# BODS has no colours, so the big groups get their brand colour (matched on the name BODS gives).
# Everyone else gets the next palette colour no other operator has, saved with their name so it
# never changes. The palette starts with colours well apart from the brand ones.
BRAND_COLOURS = [
    ("stagecoach", "#1d4f91"),
    ("first", "#6b2c91"),
    ("arriva", "#008c95"),
    ("national express", "#c8102e"),
    ("go-ahead", "#00507a"),
    ("go south coast", "#00507a"),
    ("metrobus", "#0c7a3e"),
    ("brighton & hove", "#b5121b"),
    ("oxford bus", "#7a0f3c"),
    ("transport uk", "#e4002b"),
    ("metroline", "#005eb8"),
    ("reading buses", "#5b2d8e"),
]
PALETTE = ["#c2410c", "#15803d", "#a21caf", "#0f766e", "#b45309", "#4338ca", "#9f1239", "#4d7c0f",
           "#0369a1", "#7c3aed", "#047857", "#be123c"]
DEFAULT_COLOUR = "#059669"
NAME_MAX_AGE_S = 30 * 86400
NAME_RETRY_S = 86400
# Lookups per poll, so a crowded area doesn't fire dozens of requests at once.
LOOKUPS_PER_POLL = 3


def brand_colour(name: str | None) -> str | None:
    lowered = (name or "").lower()
    return next((colour for keyword, colour in BRAND_COLOURS if keyword in lowered), None)


class OperatorNames:
    """Operator names by national operator code (NOC), from BODS's timetable listings.

    Kept in data/bus_operators.json: names are refreshed monthly, failed lookups retried daily.
    """

    def __init__(self, path: Path):
        self.path = path
        try:
            self.known: dict[str, dict] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.known = {}

    def name(self, noc: str | None) -> str | None:
        return (self.known.get(noc) or {}).get("name") if noc else None

    def colour(self, noc: str | None) -> str:
        entry = self.known.get(noc) or {} if noc else {}
        return brand_colour(entry.get("name")) or entry.get("colour") or DEFAULT_COLOUR

    def _free_colour(self, noc: str) -> str:
        taken = {e.get("colour") for k, e in self.known.items() if k != noc}
        free = [c for c in PALETTE if c not in taken]
        # More operators than colours: share, but steadily.
        return free[0] if free else PALETTE[int(hashlib.sha256(noc.encode()).hexdigest(), 16) % len(PALETTE)]

    def due(self, nocs, now: float) -> list[str]:
        out = []
        for noc in sorted({n for n in nocs if n}):
            entry = self.known.get(noc)
            age = now - entry["fetched"] if entry else None
            if entry is None or age > (NAME_MAX_AGE_S if entry.get("name") else NAME_RETRY_S):
                out.append(noc)
        return out

    async def look_up(self, client: httpx.AsyncClient, api_key: str, noc: str, now: float) -> None:
        resp = await _get(client, DATASET_URL, {"api_key": api_key, "noc": noc, "status": "published", "limit": 1})
        try:
            results = resp.json().get("results") or []
            name = (results[0].get("operatorName") or "").strip() or None if results else None
        except (ValueError, AttributeError):
            name = None
        previous = self.known.get(noc) or {}
        colour = previous.get("colour") or (None if brand_colour(name) else self._free_colour(noc))
        self.known[noc] = {"name": name, "fetched": now, "colour": colour}
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.known, indent=1, sort_keys=True))
        os.replace(tmp, self.path)


async def check_bods_key(client: httpx.AsyncClient, api_key: str) -> str | None:
    """None if BODS accepts the key, else why not (asked for a tiny area to keep it cheap)."""
    try:
        await fetch_buses(client, api_key, bounding_box(51.5, -0.12, 0.2))
    except BusFeedError as exc:
        return f"{exc}." if "rejected" in str(exc) else f"Couldn't check the BODS key: {exc}."
    return None


class BusTracker:
    """Polls BODS for the area around home and keeps the current vehicles."""

    def __init__(self, client: httpx.AsyncClient, api_key: str, lat: float, lon: float, radius_km: float,
                 interval: float, data_dir: Path):
        self.client = client
        self.operators = OperatorNames(data_dir / "bus_operators.json")
        self.api_key = api_key
        self.home = (lat, lon)
        self.radius_km = radius_km
        self.interval = interval
        self.box = bounding_box(lat, lon, radius_km)
        self.buses: list[dict] = []
        self.status: dict = {"ok": None, "last_update": None, "count": 0, "error": None, "timetable": None}
        # Set once the timetable is loaded (see bus_timetable.py); buses show without it.
        self.timetable = None
        # Set when disruptions are on (see bus_disruptions.py).
        self.disruptions = None
        # Routes seen running near home, for disruptions that name no stops.
        self.routes_seen: set[tuple] = set()
        self._timetable_routes: tuple = (None, set())

    async def poll_once(self, now: float | None = None) -> None:
        xml = await fetch_buses(self.client, self.api_key, self.box)
        try:
            buses = parse_siri_vm(xml, now)
        except ET.ParseError:
            raise BusFeedError("BODS sent data that isn't valid SIRI-VM XML") from None
        # The box's corners reach beyond the radius; keep the circle. Nearest first, for the table.
        for b in buses:
            b["distance_km"] = round(haversine_km(*self.home, b["lat"], b["lon"]), 2)
            b["direction"] = compass(bearing_deg(*self.home, b["lat"], b["lon"]))
        buses = [b for b in buses if b["distance_km"] <= self.radius_km]
        await self._learn_operators(buses, time.time() if now is None else now)
        self.routes_seen |= {(b["operator"], b["line"]) for b in buses}
        if self.disruptions:
            self.disruptions.refresh(self.routes(), now)
        for b in buses:
            b["operator_name"] = self.operators.name(b["operator"])
            b["colour"] = self.operators.colour(b["operator"])
            b["trip"] = self.timetable.progress(b) if self.timetable else None
            b["disruptions"] = self.disruptions.for_bus(b) if self.disruptions else []
        self.buses = sorted(buses, key=lambda b: b["distance_km"])
        self.status.update(ok=True, last_update=time.time(), count=len(self.buses), error=None)

    async def _learn_operators(self, buses: list[dict], now: float) -> None:
        """Look up a few operators we don't have a name for yet; buses still show meanwhile."""
        for noc in self.operators.due((b["operator"] for b in buses), now)[:LOOKUPS_PER_POLL]:
            try:
                await self.operators.look_up(self.client, self.api_key, noc, now)
            except BusFeedError as exc:
                log.info("Bus operator %s: name lookup failed (%s)", noc, exc)
                return  # BODS is struggling: try again next poll

    def routes(self) -> set[tuple]:
        """(operator, route) pairs running near home: the timetable's, and any seen live."""
        if self.timetable is not None and self._timetable_routes[0] is not self.timetable:
            self._timetable_routes = (self.timetable, {(t[0], t[1]) for t in self.timetable.trips.values()})
        return self._timetable_routes[1] | self.routes_seen

    def snapshot(self) -> dict:
        snap = {"status": self.status, "radius_km": self.radius_km, "buses": self.buses}
        if self.disruptions:
            snap["disruptions"] = self.disruptions.items
            snap["status"] = {**self.status, "disruptions": self.disruptions.status}
        return snap
