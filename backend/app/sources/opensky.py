"""OpenSky Network REST API — aircraft state vectors within a bounding box.

Docs: https://openskynetwork.github.io/opensky-api/rest.html
"""

import logging
import time
from typing import Any

import httpx

from ..geo import bounding_box
from ..models import M_TO_FT, MS_TO_FPM, MS_TO_KT, Aircraft
from .base import AircraftSource

log = logging.getLogger(__name__)

API_URL = "https://opensky-network.org/api/states/all"
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)

# Index of each field in an OpenSky state vector array.
ICAO24, CALLSIGN, COUNTRY, TIME_POS, LAST_CONTACT, LON, LAT, BARO_ALT, ON_GROUND, \
    VELOCITY, TRACK, VERT_RATE, _SENSORS, GEO_ALT, SQUAWK = range(15)

CATEGORIES = {
    2: "A1", 3: "A2", 4: "A3", 5: "A4", 6: "A5", 7: "A6", 8: "A7",
    9: "B1", 10: "B2", 11: "B3", 12: "B4", 14: "B6", 15: "B7",
}


def _scale(value: float | None, factor: float) -> float | None:
    return None if value is None else value * factor


def parse_state(state: list[Any]) -> Aircraft:
    altitude_m = state[BARO_ALT] if state[BARO_ALT] is not None else state[GEO_ALT]
    category = state[17] if len(state) > 17 else None
    return Aircraft(
        icao24=state[ICAO24].lower(),
        source="opensky",
        last_seen=float(state[LAST_CONTACT] or time.time()),
        # Last contact can be newer than the position itself; this is when lat/lon were measured.
        position_time=float(state[TIME_POS]) if state[TIME_POS] else None,
        callsign=(state[CALLSIGN] or "").strip() or None,
        origin_country=state[COUNTRY],
        category=CATEGORIES.get(category),
        lat=state[LAT],
        lon=state[LON],
        altitude_ft=_scale(altitude_m, M_TO_FT),
        on_ground=bool(state[ON_GROUND]),
        speed_kt=_scale(state[VELOCITY], MS_TO_KT),
        track_deg=state[TRACK],
        vertical_rate_fpm=_scale(state[VERT_RATE], MS_TO_FPM),
        squawk=state[SQUAWK],
    )


class OpenSkySource(AircraftSource):
    name = "opensky"
    priority = 0

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        lat: float,
        lon: float,
        radius_km: float,
        interval: float,
        client_id: str = "",
        client_secret: str = "",
    ):
        super().__init__(client)
        self.interval = interval
        self.bbox = bounding_box(lat, lon, radius_km)
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires = 0.0

    async def _auth_headers(self) -> dict[str, str]:
        if not (self.client_id and self.client_secret):
            return {}
        if self._token is None or time.time() > self._token_expires - 60:
            resp = await self.client.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            self._token = body["access_token"]
            self._token_expires = time.time() + body.get("expires_in", 1800)
        return {"Authorization": f"Bearer {self._token}"}

    async def fetch(self) -> list[Aircraft]:
        lamin, lomin, lamax, lomax = self.bbox
        resp = await self.client.get(
            API_URL,
            params={
                "lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax,
                "extended": 1,
            },
            headers=await self._auth_headers(),
        )
        if resp.status_code == 429:
            retry = resp.headers.get("X-Rate-Limit-Retry-After-Seconds", "?")
            raise RuntimeError(f"OpenSky quota exhausted, retry after {retry}s")
        resp.raise_for_status()
        states = resp.json().get("states") or []
        return [parse_state(s) for s in states]
