"""Airport reference data from the OpenFlights dataset (downloaded once, cached)."""

import csv
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .geo import haversine_km

log = logging.getLogger(__name__)

AIRPORTS_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"


@dataclass(frozen=True)
class Airport:
    name: str
    city: str
    country: str
    iata: str | None
    icao: str
    lat: float
    lon: float
    elevation_ft: int


def _clean(value: str) -> str | None:
    return None if value in ("", "\\N") else value


def parse_airports(lines: list[str]) -> list[Airport]:
    airports = []
    for row in csv.reader(lines):
        if len(row) < 13 or row[12] != "airport":
            continue
        icao = _clean(row[5])
        if not icao:
            continue
        try:
            airports.append(Airport(
                name=row[1], city=row[2], country=row[3], iata=_clean(row[4]), icao=icao,
                lat=float(row[6]), lon=float(row[7]), elevation_ft=int(float(row[8] or 0)),
            ))
        except ValueError:
            continue
    return airports


async def load_airports(client: httpx.AsyncClient, data_dir: Path) -> list[Airport]:
    path = data_dir / "airports.dat"
    if not path.exists():
        log.info("Downloading OpenFlights airport data")
        resp = await client.get(AIRPORTS_URL)
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return parse_airports(path.read_text(encoding="utf-8").splitlines())


def nearest_airports(
    airports: list[Airport], lat: float, lon: float, radius_km: float, limit: int = 15
) -> list[dict]:
    nearby = []
    for ap in airports:
        d = haversine_km(lat, lon, ap.lat, ap.lon)
        if d <= radius_km:
            nearby.append({**asdict(ap), "distance_km": round(d, 1)})
    nearby.sort(key=lambda a: a["distance_km"])
    return nearby[:limit]
