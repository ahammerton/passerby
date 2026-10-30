"""Common aircraft model every source normalises into.

Units follow aviation convention: altitude in feet, speed in knots,
vertical rate in feet per minute.
"""

from pydantic import BaseModel

M_TO_FT = 3.28084
MS_TO_KT = 1.943844
MS_TO_FPM = 196.8504


class RouteEnd(BaseModel):
    iata: str | None = None
    icao: str | None = None
    name: str | None = None
    city: str | None = None
    country: str | None = None


class Route(BaseModel):
    airline: str | None = None
    origin: RouteEnd
    destination: RouteEnd


class Aircraft(BaseModel):
    icao24: str  # 24-bit transponder address, lowercase hex
    source: str  # name of the AircraftSource that reported it
    last_seen: float  # unix time of the most recent update
    # When lat/lon were measured, if known: browsers extrapolate movement from here.
    position_time: float | None = None

    callsign: str | None = None
    registration: str | None = None
    type_code: str | None = None
    origin_country: str | None = None
    category: str | None = None

    lat: float | None = None
    lon: float | None = None
    altitude_ft: float | None = None
    on_ground: bool = False
    speed_kt: float | None = None
    track_deg: float | None = None
    vertical_rate_fpm: float | None = None
    squawk: str | None = None
    military: bool = False

    # Filled in by the enricher from adsbdb.com.
    aircraft_name: str | None = None  # e.g. "Boeing 777 236ER"
    operator: str | None = None
    route: Route | None = None

    # From the callsign, by airlines.identify(). `livery` is {"body", "tail"} colours when known.
    airline_icao: str | None = None
    airline_name: str | None = None
    livery: dict[str, str] | None = None

    # Map icon and a plain-English description, from aircraft_types.classify().
    shape: str | None = None
    kind: str | None = None

    # Filled in by the aggregator relative to the home location.
    distance_km: float | None = None
    bearing_deg: float | None = None
    direction: str | None = None
    overhead: bool = False

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None
