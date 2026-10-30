"""UK railway station locations (downloaded once, cached).

Data: https://github.com/davwheat/uk-railway-stations — derived from Trainline EU's
stations dataset, Open Database License (ODbL). Attribution is shown in the UI.
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from ..geo import haversine_km

log = logging.getLogger(__name__)

STATIONS_URL = "https://raw.githubusercontent.com/davwheat/uk-railway-stations/main/stations.json"


@dataclass(frozen=True)
class Station:
    crs: str
    name: str
    lat: float
    lon: float

    def as_dict(self) -> dict:
        return asdict(self)


def parse_stations(raw: list[dict]) -> dict[str, Station]:
    stations = {}
    for s in raw:
        try:
            crs = s["crsCode"].upper()
            stations[crs] = Station(crs=crs, name=s["stationName"], lat=float(s["lat"]), lon=float(s["long"]))
        except (KeyError, TypeError, ValueError):
            continue
    return stations


async def load_stations(client: httpx.AsyncClient, data_dir: Path) -> dict[str, Station]:
    path = data_dir / "stations.json"
    if not path.exists():
        log.info("Downloading UK station data")
        resp = await client.get(STATIONS_URL)
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return parse_stations(json.loads(path.read_text(encoding="utf-8")))


def stations_within(stations: dict[str, Station], lat: float, lon: float, radius_km: float) -> list[Station]:
    """Stations within radius_km of a point, nearest first."""
    distances = ((haversine_km(lat, lon, s.lat, s.lon), s) for s in stations.values())
    return [s for d, s in sorted(distances, key=lambda x: x[0]) if d <= radius_km]
