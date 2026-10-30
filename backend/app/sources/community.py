"""Community ADS-B networks exposing the readsb "v2" API (adsb.lol, airplanes.live).

These are unfiltered (military included) and update every few seconds.
- adsb.lol:        https://api.adsb.lol/docs  (open, ODbL data licence)
- airplanes.live:  https://airplanes.live/api-guide/ (contact them before use)
"""

import httpx

from ..models import Aircraft
from .base import AircraftSource
from .readsb import parse_response

KM_PER_NM = 1.852
MAX_RADIUS_NM = 250  # both APIs cap point queries at 250 nm

ADSBLOL_URL = "https://api.adsb.lol"
AIRPLANESLIVE_URL = "https://api.airplanes.live"


class CommunitySource(AircraftSource):
    # Fresher than OpenSky, but your own receiver still wins.
    priority = 5

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        name: str,
        base_url: str,
        lat: float,
        lon: float,
        radius_km: float,
        interval: float,
    ):
        super().__init__(client)
        self.name = name
        self.interval = interval
        radius_nm = min(round(radius_km / KM_PER_NM), MAX_RADIUS_NM)
        self.url = f"{base_url}/v2/point/{lat}/{lon}/{radius_nm}"

    async def fetch(self) -> list[Aircraft]:
        resp = await self.client.get(self.url)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return parse_response(body, self.name)
