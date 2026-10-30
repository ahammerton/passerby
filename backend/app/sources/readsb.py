"""Local ADS-B receiver via its aircraft.json (readsb, tar1090, dump1090-fa, or the older
dump1090-mutability format used by Flightradar24 and PiAware feeders).

Formats: https://github.com/wiedehopf/readsb/blob/dev/README-json.md (alt_baro, gs, baro_rate)
and the older dump1090 one (altitude, speed, vert_rate).
"""

import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..models import Aircraft
from .base import AircraftSource

# Where common receiver packages serve aircraft.json, tried in order for a bare host or IP.
RECEIVER_PATHS = [
    "/tar1090/data/aircraft.json",
    "/skyaware/data/aircraft.json",
    "/dump1090-fa/data/aircraft.json",
    "/dump1090/data/aircraft.json",
    "/data/aircraft.json",
    ":8080/data/aircraft.json",
]
# A receiver clock further out than this is ignored in favour of ours.
MAX_CLOCK_SKEW_S = 5


def _first(entry: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if entry.get(k) is not None:
            return entry[k]
    return None


def parse_aircraft(entry: dict[str, Any], now: float, source: str = "receiver") -> Aircraft | None:
    hex_id = entry.get("hex", "")
    # A leading "~" marks a non-ICAO (TIS-B) address; skip those.
    if not hex_id or hex_id.startswith("~"):
        return None
    # Newer readsb / dump1090-fa use alt_baro; older dump1090 builds use altitude.
    alt_baro = _first(entry, "alt_baro", "altitude")
    on_ground = alt_baro == "ground"
    altitude = entry.get("alt_geom") if on_ground or alt_baro is None else alt_baro
    seen = entry.get("seen_pos", entry.get("seen", 0))
    position_time = now - float(entry["seen_pos"]) if "seen_pos" in entry else None
    return Aircraft(
        icao24=hex_id.lower(),
        source=source,
        last_seen=now - float(seen),
        position_time=position_time,
        callsign=(entry.get("flight") or "").strip() or None,
        registration=entry.get("r"),
        type_code=entry.get("t"),
        category=entry.get("category"),
        lat=entry.get("lat"),
        lon=entry.get("lon"),
        altitude_ft=0.0 if on_ground else altitude,
        on_ground=on_ground,
        speed_kt=_first(entry, "gs", "speed"),
        track_deg=entry.get("track"),
        vertical_rate_fpm=_first(entry, "baro_rate", "geom_rate", "vert_rate"),
        squawk=entry.get("squawk"),
        # dbFlags bit 0 marks military aircraft in the readsb database.
        military=bool(entry.get("dbFlags", 0) & 1),
    )


def parse_response(body: dict[str, Any], source: str, local_now: float | None = None) -> list[Aircraft]:
    """Parse a readsb-style response: local aircraft.json or a v2 community API.

    With `local_now`, a sender clock that's badly out is replaced by ours.
    """
    now = float(body.get("now", 0))
    # Community APIs report "now" in milliseconds, local readsb in seconds.
    if now > 1e11:
        now /= 1000
    if local_now is not None and abs(now - local_now) > MAX_CLOCK_SKEW_S:
        now = local_now
    entries = body.get("aircraft", body.get("ac")) or []
    parsed = (parse_aircraft(e, now, source) for e in entries)
    return [a for a in parsed if a is not None]


def receiver_urls(address: str) -> list[str]:
    """Candidate aircraft.json URLs for what someone typed: a full URL, or just a host/IP."""
    address = address.strip().rstrip("/")
    if not address:
        return []
    if address.endswith(".json"):
        return [address if "://" in address else f"http://{address}"]
    base = address if "://" in address else f"http://{address}"
    return [base + path for path in RECEIVER_PATHS]


async def find_receiver(client: httpx.AsyncClient, address: str) -> tuple[str, int, int] | None:
    """(aircraft.json URL, aircraft seen, with a position) for the first candidate that answers."""
    for url in receiver_urls(address):
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        try:
            resp = await client.get(url, timeout=5)
            if resp.status_code != 200:
                continue
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            continue
        if isinstance(body, dict) and isinstance(body.get("aircraft"), list):
            aircraft = body["aircraft"]
            return url, len(aircraft), sum(1 for a in aircraft if "lat" in a)
    return None


class ReadsbSource(AircraftSource):
    name = "receiver"
    # Your own receiver is fresher than any online feed, so it wins on overlap.
    priority = 10
    local = True

    def __init__(self, client: httpx.AsyncClient, *, url: str, interval: float):
        super().__init__(client)
        self.url = url
        self.interval = interval

    async def fetch(self) -> list[Aircraft]:
        resp = await self.client.get(self.url)
        resp.raise_for_status()
        return parse_response(resp.json(), self.name, local_now=time.time())
