"""Community ADS-B networks exposing the readsb "v2" API (adsb.lol, airplanes.live).

These are unfiltered (military included) and update every few seconds.
- adsb.lol:        https://api.adsb.lol/docs  (open, ODbL data licence)
- airplanes.live:  https://airplanes.live/api-guide/ (contact them before use)
"""

import httpx

from ..models import Aircraft
from .base import AircraftSource, RateLimited
from .readsb import parse_response

KM_PER_NM = 1.852
MAX_RADIUS_NM = 250  # both APIs cap point queries at 250 nm

ADSBLOL_URL = "https://api.adsb.lol"
# adsb.lol allows 500 requests a day for each IP address (x-ratelimit-limit: 500;w=86400, November 2026):
# one every 173 s at most. Shorter intervals are raised to this.
ADSBLOL_MIN_INTERVAL = 180
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
        if resp.status_code == 429:
            raise rate_limited(resp)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return parse_response(body, self.name)


def rate_limited(resp: httpx.Response) -> RateLimited:
    """A 429 as a RateLimited error, with the wait the feed asks for (x-ratelimit-* or Retry-After)."""
    h = resp.headers
    wait = None
    for name in ("retry-after", "x-ratelimit-reset"):
        try:
            wait = float(h[name])
            break
        except (KeyError, ValueError):
            continue
    limit = (h.get("x-ratelimit-limit") or "").split(",")[0].strip()
    daily = h.get("x-ratelimit-remaining") == "0" and "w=86400" in (h.get("x-ratelimit-limit") or "")
    if daily:
        return RateLimited(f"Daily limit reached ({limit} requests a day)", wait)
    return RateLimited("Rate limited", wait)
