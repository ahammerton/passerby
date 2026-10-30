"""Road closures and incidents on motorways and major A-roads, from National Highways.

https://developer.data.nationalhighways.co.uk: a free account and a subscription to
"Road & Lane Closures" give a key (sent as a header, not in the URL). DATEX II as JSON:

- planned: roadworks and other scheduled closures, on now or coming up (~4 MB for England);
  fetched every 15 minutes.
- unplanned: closures set on the motorway signs and signals, which is how incidents show
  up (a lane closed after a crash, say); small, fetched every 2 minutes.

The API allows 10 calls a minute per key. Only National Highways roads are covered, not
council roads.
"""

import asyncio
import logging
import re
import time
from datetime import datetime

import httpx

from .bus_timetable import simplify
from .geo import haversine_km

log = logging.getLogger(__name__)

URL = "https://api.data.nationalhighways.co.uk/roads/v2.0/closures"
PLANNED_INTERVAL_S = 900
UNPLANNED_INTERVAL_S = 120
UPCOMING_S = 7 * 86400
MAX_BYTES = 40_000_000
# Lines are drawn to within this of the published path.
LINE_TOLERANCE_M = 15
# active (on now) beats planned beats suspended (scheduled, not in force right now).
STATUS_ORDER = {"active": 0, "planned": 1, "suspended": 2}


class RoadsError(Exception):
    pass


def _words(value: str | None) -> str | None:
    """"roadImprovementOrUpgrading" -> "road improvement or upgrading"."""
    if not value or value in ("other", "unknown"):
        return None
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value).lower()


def _time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cause(record: dict) -> str | None:
    detail = (record.get("cause") or {}).get("detailedCauseType") or {}
    for value in detail.values():
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, list):
            value = value[0] if value else None
        if words := _words(value if isinstance(value, str) else None):
            return words
    return _words((record.get("cause") or {}).get("causeType"))


def _locations(record: dict) -> list[dict]:
    ref = record.get("locationReference") or {}
    groups = (ref.get("locLocationGroupByList") or {}).get("locationContainedInGroup") or [ref]
    out = []
    for g in groups:
        linear = g.get("locLinearLocation") or {}
        pos = ((linear.get("gmlLineString") or {}).get("locGmlLineString") or {}).get("posList") or ""
        try:
            v = [float(x) for x in pos.split()]
        except ValueError:
            continue
        points = [(a, b) for a, b in zip(v[::2], v[1::2]) if -90 <= a <= 90 and -180 <= b <= 180]
        if not points:
            continue
        roads = [(e.get("linearElement") or {}).get("locLinearElementByCode", {}).get("roadName")
                 for e in (g.get("locSingleRoadLinearLocation") or {}).get("linearWithinLinearElement") or []]
        out.append({
            "description": (linear.get("supplementaryPositionalDescription") or {}).get("locationDescription"),
            "roads": [r for r in roads if r],
            "points": points,
        })
    return out


def parse(payload: dict, kind: str) -> list[dict]:
    """One item per situation (its records are the parts of one closure)."""
    items = []
    for sit in (payload.get("D2Payload") or {}).get("situation") or []:
        records = [next(iter(r.values())) for r in sit.get("situationRecord") or [] if r]
        if not records:
            continue
        statuses, starts, ends, comments, causes, types, locations = [], [], [], [], [], [], []
        for r in records:
            validity = r.get("validity") or {}
            span = validity.get("validityTimeSpecification") or {}
            statuses.append(validity.get("validityStatus") or "planned")
            starts.append(_time(span.get("overallStartTime")))
            ends.append(_time(span.get("overallEndTime")))
            for c in r.get("generalPublicComment") or []:
                text = (c.get("comment") or "").strip()
                # Unplanned ones only repeat their type ("laneClosures") as the comment.
                if text and " " in text and text not in comments:
                    comments.append(text)
            if (cause := _cause(r)) and cause not in causes:
                causes.append(cause)
            management = _words(((r.get("roadOrCarriagewayOrLaneManagementType") or {}).get("value")))
            if management and management not in types:
                types.append(management)
            for loc in _locations(r):
                # A closure repeated on several nights lists the same stretch for each.
                if not any(loc["points"] == seen["points"] for seen in locations):
                    locations.append(loc)
        if not locations:
            continue
        descriptions = list(dict.fromkeys(loc["description"] for loc in locations if loc["description"]))
        roads = list(dict.fromkeys(r for loc in locations for r in loc["roads"]))
        items.append({
            "id": f"{kind}:{sit.get('idG')}",
            "kind": kind,
            "status": min(statuses, key=lambda s: STATUS_ORDER.get(s, 3)),
            "start": min((s for s in starts if s), default=None),
            "end": max((e for e in ends if e), default=None),
            "summary": comments[0] if comments else (descriptions[0] if descriptions else "Closure"),
            "comments": comments[:5],
            "cause": causes[0] if causes else None,
            "type": types[0] if types else None,
            "roads": roads,
            "places": descriptions[:10],
            "locations": locations,
        })
    return items


def local(items: list[dict], lat: float, lon: float, radius_km: float, now: float) -> list[dict]:
    """Closures within the radius, on now or within a week; their lines simplified for the map."""
    out = []
    for item in items:
        if item["end"] and item["end"] < now:
            continue
        if item["status"] != "active" and item["start"] and item["start"] - now > UPCOMING_S:
            continue
        near = [loc for loc in item["locations"]
                if min(haversine_km(lat, lon, a, b) for a, b in loc["points"]) <= radius_km]
        if not near:
            continue
        lines = [simplify(loc["points"], LINE_TOLERANCE_M) for loc in near]
        out.append({**{k: v for k, v in item.items() if k != "locations"}, "lines": lines,
                    "active": item["status"] == "active" or bool(item["start"] and item["start"] <= now
                                                                   and (not item["end"] or item["end"] >= now))})
    # Incidents first, then what's on now, then the rest by start time.
    return sorted(out, key=lambda i: (i["kind"] != "unplanned", not i["active"], i["start"] or 0))


async def fetch(client: httpx.AsyncClient, api_key: str, kind: str) -> dict:
    params = {"closureType": kind}
    headers = {"Ocp-Apim-Subscription-Key": api_key, "X-Response-MediaType": "application/json"}
    try:
        resp = await client.get(URL, params=params, headers=headers, timeout=60)
    except httpx.TimeoutException:
        raise RoadsError("National Highways didn't answer in time") from None
    except httpx.HTTPError as exc:
        raise RoadsError(f"couldn't reach National Highways ({type(exc).__name__})") from None
    if resp.status_code in (401, 403):
        raise RoadsError("National Highways rejected the API key")
    if resp.status_code == 429:
        raise RoadsError("National Highways rate limit reached")
    if resp.status_code != 200:
        raise RoadsError(f"National Highways answered {resp.status_code}")
    if len(resp.content) > MAX_BYTES:
        raise RoadsError("National Highways sent an unexpectedly large response")
    try:
        return resp.json()
    except ValueError:
        raise RoadsError("National Highways sent data that isn't JSON") from None


async def check_key(client: httpx.AsyncClient, api_key: str) -> str | None:
    """None if National Highways accepts the key, else why not (the small unplanned list)."""
    try:
        await fetch(client, api_key, "unplanned")
    except RoadsError as exc:
        return f"{exc}." if "rejected" in str(exc) else f"Couldn't check the National Highways key: {exc}."
    return None


class Roads:
    """Closures near home: planned every 15 minutes, unplanned every 2."""

    def __init__(self, client: httpx.AsyncClient, api_key: str, lat: float, lon: float, radius_km: float):
        self.client, self.api_key = client, api_key
        self.home, self.radius_km = (lat, lon), radius_km
        self.all = {"planned": [], "unplanned": []}
        self.items: list[dict] = []
        self.status: dict = {"ok": None, "last_update": None, "count": 0, "error": None}

    async def update(self, kind: str, now: float | None = None) -> None:
        self.all[kind] = parse(await fetch(self.client, self.api_key, kind), kind)
        self.refresh(now)
        self.status.update(ok=True, last_update=time.time(), error=None)

    def refresh(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.items = local(self.all["planned"] + self.all["unplanned"], *self.home, self.radius_km, now)
        self.status["count"] = len(self.items)

    def snapshot(self) -> dict:
        return {"status": self.status, "radius_km": self.radius_km, "items": self.items}


async def run(roads: Roads) -> None:
    """Fetch both lists on their own schedules, within the API's 10 calls a minute."""
    due = {"planned": 0.0, "unplanned": 0.0}
    intervals = {"planned": PLANNED_INTERVAL_S, "unplanned": UNPLANNED_INTERVAL_S}
    while True:
        now = time.time()
        for kind in due:
            if now >= due[kind]:
                try:
                    await roads.update(kind)
                    due[kind] = now + intervals[kind]
                except RoadsError as exc:
                    log.warning("Roads (%s): %s", kind, exc)
                    roads.status.update(ok=False, error=str(exc))
                    due[kind] = now + max(120, intervals[kind] / 3)
                except Exception as exc:
                    log.warning("Roads (%s): unexpected %s", kind, type(exc).__name__)
                    roads.status.update(ok=False, error=f"unexpected {type(exc).__name__}")
                    due[kind] = now + intervals[kind]
        roads.refresh()  # closures start and end between fetches
        await asyncio.sleep(min(60.0, max(5.0, min(due.values()) - time.time())))
