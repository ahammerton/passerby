"""Port names for AIS destinations.

Ships are asked to give their destination as a UN/LOCODE: a country code and a 3-letter
place code, like GBSOU for Southampton. Many do, sometimes as a route ("BEANR > GBDAG",
"GBPME<>GBFBU") or with a space ("GB SOU"). Others type a name ("LONDON", "SOTON-COWES"),
which is shown as it is: a destination is only translated when all of it is codes.

The code list (about 17,000 ports and multimodal sites) comes from the UN/LOCODE dataset
mirrored at https://github.com/datasets/un-locode, and is cached in data/ports.json.gz.
"""

import csv
import gzip
import io
import json
import logging
import re
import time
from pathlib import Path

import httpx

from ..geo import haversine_km

log = logging.getLogger(__name__)

URL = "https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv"
MAX_AGE = 90 * 86400
RETRY_SECONDS = 6 * 3600

# Small local craft (tugs, pilots, ferries, workboats) don't sail to the other side of the
# world: a far-away match for their destination is a coincidence ("TH KSI" on the Thames
# is not Ko Sichang in Thailand).
LOCAL_CRAFT_MAX_M = 60
LOCAL_CRAFT_MAX_KM = 1500

CODE = r"[A-Z]{2} ?[A-Z2-9]{3}"
SEPARATOR = r"\s*(?:<?[-=]*>|[-/]|TO)\s*"  # >, ->, <>, <=>, -, /, TO
ROUTE = re.compile(rf"\s*({CODE})(?:{SEPARATOR}({CODE}))?\s*")
ROUND_TRIP = re.compile(r"<[-=]?>")


def _coordinates(text: str) -> tuple[float, float] | None:
    """'5054N 00124W' -> (50.9, -1.4)."""
    m = re.fullmatch(r"(\d{2})(\d{2})([NS]) (\d{3})(\d{2})([EW])", text.strip())
    if not m:
        return None
    lat = int(m[1]) + int(m[2]) / 60
    lon = int(m[4]) + int(m[5]) / 60
    return (-lat if m[3] == "S" else lat), (-lon if m[6] == "W" else lon)


def parse_code_list(text: str) -> dict[str, list]:
    """Code -> [name, lat, lon] (lat/lon None when not given), for ports and multimodal sites."""
    ports = {}
    for row in csv.DictReader(io.StringIO(text)):
        function = row.get("Function") or ""
        if row.get("Change") == "X" or not ("1" in function[:1] or "6" in function[5:6]):
            continue
        code = (row.get("Country") or "") + (row.get("Location") or "")
        if len(code) != 5:
            continue
        where = _coordinates(row.get("Coordinates") or "") or (None, None)
        ports[code] = [row["Name"], *(round(v, 3) if v is not None else None for v in where)]
    return ports


class Ports:
    def __init__(self, ports: dict[str, list] | None = None):
        self.ports = ports or {}
        self._cache: dict[str, tuple[list[dict], bool] | None] = {}

    def __len__(self) -> int:
        return len(self.ports)

    def route(self, destination: str | None) -> tuple[list[dict], bool] | None:
        """([{code, name, country, lat, lon}...], round trip) when the whole destination is port
        codes, else None."""
        if not destination or not self.ports:
            return None
        if destination not in self._cache:
            self._cache[destination] = self._route(destination.upper())
            if len(self._cache) > 5000:
                self._cache.clear()
        return self._cache[destination]

    def _route(self, text: str) -> tuple[list[dict], bool] | None:
        m = ROUTE.fullmatch(text)
        if not m:
            return None
        stops = []
        for code in filter(None, m.groups()):
            code = code.replace(" ", "")
            found = self.ports.get(code)
            if not found:
                return None
            name, lat, lon = found
            stops.append({"code": code, "name": name, "country": code[:2], "lat": lat, "lon": lon})
        return stops, bool(ROUND_TRIP.search(text))

    def describe(self, destination: str | None, ship: dict) -> dict | None:
        """The destination as ports, or None to show it as typed. `ship` needs lat, lon and length_m
        (and category)."""
        found = self.route(destination)
        if not found:
            return None
        stops, round_trip = found
        local = (ship.get("length_m") or 0) < LOCAL_CRAFT_MAX_M or ship.get("category") in {
            "tug", "fishing", "pleasure", "sailing", "small", "special"}
        if local and any(s["lat"] is not None and haversine_km(ship["lat"], ship["lon"], s["lat"], s["lon"])
                         > LOCAL_CRAFT_MAX_KM for s in stops):
            return None
        return {"stops": [{k: s[k] for k in ("code", "name", "country")} for s in stops], "round_trip": round_trip}


def _read(path: Path) -> dict[str, list]:
    return json.loads(gzip.decompress(path.read_bytes()))


async def load_ports(client: httpx.AsyncClient, data_dir: Path) -> Ports:
    """The cached code list, downloaded when missing or 90 days old."""
    path = data_dir / "ports.json.gz"
    if path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
        return Ports(_read(path))
    try:
        log.info("Downloading UN/LOCODE port names")
        resp = await client.get(URL, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        ports = parse_code_list(resp.text)
        if len(ports) < 1000:
            raise ValueError(f"only {len(ports)} ports in the download")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(json.dumps(ports, separators=(",", ":")).encode()))
        return Ports(ports)
    except (httpx.HTTPError, ValueError) as exc:
        if path.exists():
            log.warning("Port names refresh failed, using the cached copy: %s", exc)
            return Ports(_read(path))
        raise
