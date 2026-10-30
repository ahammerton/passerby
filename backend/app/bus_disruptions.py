"""Bus disruptions from BODS's SIRI-SX feed: diversions, stop closures, road closures, events.

The feed covers all of England (~600 situations, ~7 MB) and needs the same API key as the
live positions, so it's fetched every 10 minutes and cut down to what affects the area:

- A situation that names its stops (most do, with coordinates) counts if any is within the
  bus radius of home. Operators reuse route numbers across towns (Stagecoach South has a
  "3" in Guildford and another in Andover), so a route match alone isn't enough here.
- One that names no stops counts if at least half the routes it names run near home (from
  the timetable, or the buses seen so far). Half, because route numbers repeat: an event in
  Alton on Stagecoach's 13, 64 and 65 isn't Fareham's just because a 13 runs there too.

Only what's on now, or starts within a week, is kept.
"""

import re
import time
import xml.etree.ElementTree as ET

import httpx

from .buses import NS, BusFeedError, _get, _place, _text, _timestamp
from .geo import haversine_km

SIRI_SX_URL = "https://data.bus-data.dft.gov.uk/api/v1/siri-sx/"
INTERVAL_S = 600
UPCOMING_S = 7 * 86400
MAX_TEXT = 2000
MAX_STOPS = 50


def _reason(situation: ET.Element) -> str | None:
    """"roadClosed" -> "road closed", from whichever *Reason element it has."""
    for child in situation:
        if child.tag.endswith("Reason") and (child.text or "").strip():
            words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", child.text.strip()).lower()
            return None if words in ("unknown", "undefined problem") else words
    return None


def _link(situation: ET.Element) -> str | None:
    uri = _text(situation, "s:InfoLinks/s:InfoLink/s:Uri")
    return uri if uri and uri.startswith("https://") else None


def parse_siri_sx(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for sit in root.iterfind(".//s:PtSituationElement", NS):
        if (_text(sit, "s:Progress") or "open") == "closed":
            continue
        periods = [{"start": _timestamp(_text(p, "s:StartTime")), "end": _timestamp(_text(p, "s:EndTime"))}
                   for p in sit.iterfind("s:ValidityPeriod", NS)]
        lines, stops, seen_lines, seen_stops = [], [], set(), set()
        for line in sit.iterfind(".//s:AffectedLine", NS):
            key = (_text(line, "s:AffectedOperator/s:OperatorRef"),
                   _text(line, "s:PublishedLineName") or _text(line, "s:LineRef"))
            if key[1] and key not in seen_lines:
                seen_lines.add(key)
                lines.append({"operator": key[0], "operator_name": _text(line, "s:AffectedOperator/s:OperatorName"),
                              "line": key[1]})
        for stop in sit.iterfind(".//s:AffectedStopPoint", NS):
            try:
                lat = float(_text(stop, "s:Location/s:Latitude") or "nan")
                lon = float(_text(stop, "s:Location/s:Longitude") or "nan")
            except ValueError:
                continue
            ref = _text(stop, "s:StopPointRef") or f"{lat},{lon}"
            if -90 <= lat <= 90 and -180 <= lon <= 180 and ref not in seen_stops:
                seen_stops.add(ref)
                stops.append({"name": _place(_text(stop, "s:StopPointName")), "lat": lat, "lon": lon})
        out.append({
            "id": _text(sit, "s:SituationNumber") or f"sx{len(out)}",
            "summary": (_text(sit, "s:Summary") or "Disruption")[:300],
            "description": (_text(sit, "s:Description") or "")[:MAX_TEXT] or None,
            "reason": _reason(sit),
            "planned": _text(sit, "s:Planned") == "true",
            "severity": _text(sit, ".//s:Consequence/s:Severity"),
            "periods": periods,
            "lines": lines,
            "stops": stops,
            "link": _link(sit),
        })
    return out


def when(d: dict, now: float) -> tuple[bool, float | None, float | None]:
    """(on now, when it next starts if not, when the current or next period ends)."""
    periods = d["periods"] or [{"start": None, "end": None}]
    for p in periods:
        if (p["start"] is None or p["start"] <= now) and (p["end"] is None or p["end"] >= now):
            return True, None, p["end"]
    upcoming = sorted((p for p in periods if p["start"] and p["start"] > now), key=lambda p: p["start"])
    return (False, upcoming[0]["start"], upcoming[0]["end"]) if upcoming else (False, None, None)


def local(situations: list[dict], lat: float, lon: float, radius_km: float, routes: set[tuple],
          now: float) -> list[dict]:
    """The situations affecting the area, on now or starting within a week, soonest first."""
    out = []
    for d in situations:
        active, starts, ends = when(d, now)
        if not active and not (starts and starts - now <= UPCOMING_S):
            continue
        if d["stops"]:
            near = [s for s in d["stops"] if haversine_km(lat, lon, s["lat"], s["lon"]) <= radius_km]
            if not near:
                continue
            stops = near[:MAX_STOPS]
        elif d["lines"] and 2 * sum((l["operator"], l["line"]) in routes for l in d["lines"]) >= len(d["lines"]):
            stops = []
        else:
            continue
        out.append({**{k: v for k, v in d.items() if k != "periods"}, "stops": stops,
                    "active": active, "starts": starts, "ends": ends})
    return sorted(out, key=lambda d: (not d["active"], d["starts"] or 0))


class Disruptions:
    """The area's current bus disruptions, refreshed every 10 minutes."""

    def __init__(self, client: httpx.AsyncClient, api_key: str, lat: float, lon: float, radius_km: float):
        self.client, self.api_key = client, api_key
        self.home, self.radius_km = (lat, lon), radius_km
        self.situations: list[dict] = []  # all of England's, parsed
        self.items: list[dict] = []  # the local ones
        self.status: dict = {"ok": None, "last_update": None, "count": 0, "error": None}

    async def fetch(self) -> None:
        xml = (await _get(self.client, SIRI_SX_URL, {"api_key": self.api_key})).content
        try:
            self.situations = parse_siri_sx(xml)
        except ET.ParseError:
            raise BusFeedError("BODS sent disruptions that aren't valid SIRI-SX XML") from None
        self.status.update(ok=True, last_update=time.time(), error=None)

    def refresh(self, routes: set[tuple], now: float | None = None) -> None:
        """Re-pick the local ones (cheap): routes can grow as the timetable loads."""
        self.items = local(self.situations, *self.home, self.radius_km, routes,
                           time.time() if now is None else now)
        self.status["count"] = len(self.items)

    def for_bus(self, bus: dict) -> list[str]:
        """IDs of disruptions on now that affect this bus's route."""
        key = (bus.get("operator"), bus.get("line"))
        return [d["id"] for d in self.items if d["active"] and any((l["operator"], l["line"]) == key for l in d["lines"])]
