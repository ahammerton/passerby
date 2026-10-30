"""What the settings page can change, and how changes are validated.

Logins can be changed here too, but passwords and secrets are write-only: the
app has no authentication, so they are never sent back to the browser. The page
only learns whether each one is set.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings

# Changing these moves or resizes the train area: track and timetable are fetched again.
TRAIN_AREA_KEYS = {"home_lat", "home_lon", "train_radius_km"}
# Changing these changes where aircraft come from.
RECEIVER_KEYS = {"readsb_enabled", "readsb_url"}
# Never returned to the browser.
SECRET_KEYS = {"nrod_password", "opensky_client_secret", "bods_api_key", "national_highways_api_key"}
NROD_KEYS = {"nrod_username", "nrod_password"}
OPENSKY_KEYS = {"opensky_client_id", "opensky_client_secret"}


def _codes(value: str) -> list[str]:
    return [c.strip().upper() for c in value.split(",") if c.strip()]


class SettingsUpdate(BaseModel):
    """A partial update from the settings page; omitted fields are left as they are."""

    model_config = ConfigDict(extra="forbid")

    home_name: str | None = Field(None, min_length=1, max_length=40)
    home_lat: float | None = Field(None, ge=-90, le=90)
    home_lon: float | None = Field(None, ge=-180, le=180)

    # The community ADS-B APIs cap queries at 250 nm, and larger OpenSky boxes cost more credits.
    radius_km: float | None = Field(None, ge=5, le=250)
    alert_radius_km: float | None = Field(None, gt=0, le=50)
    alert_min_altitude_ft: float | None = Field(None, ge=0, le=20000)

    # Your own ADS-B receiver: an IP/host (its aircraft.json is found on save) or the full URL.
    readsb_enabled: bool | None = None
    readsb_url: str | None = Field(None, max_length=300)
    online_feeds: Literal["fill", "backup"] | None = None

    distance_unit: Literal["km", "mi", "nm"] | None = None
    altitude_unit: Literal["ft", "m"] | None = None
    speed_unit: Literal["kt", "mph", "kmh"] | None = None
    aircraft_colours: Literal["airline", "altitude"] | None = None
    interesting_alerts: Literal["all", "emergencies", "off"] | None = None

    train_radius_km: float | None = Field(None, ge=1, le=30)
    train_watch: str | None = None
    train_boards: str | None = None
    train_alert_seconds: int | None = Field(None, ge=10, le=600)
    passing_window: bool | None = None
    passing_minutes: int | None = Field(None, ge=1, le=30)

    buses_enabled: bool | None = None
    bus_radius_km: float | None = Field(None, ge=1, le=30)
    bus_min_zoom: int | None = Field(None, ge=10, le=18)
    bus_delays: bool | None = None
    bus_disruptions: bool | None = None

    roads_enabled: bool | None = None
    road_radius_km: float | None = Field(None, ge=1, le=50)
    bus_region: Literal["auto", "south_east", "london", "south_west", "east_anglia", "east_midlands",
                        "west_midlands", "north_west", "north_east", "yorkshire"] | None = None

    # Logins. An empty string clears one; leaving a field out keeps it.
    nrod_username: str | None = Field(None, max_length=200)
    nrod_password: str | None = Field(None, max_length=200)
    opensky_client_id: str | None = Field(None, max_length=200)
    opensky_client_secret: str | None = Field(None, max_length=200)
    bods_api_key: str | None = Field(None, max_length=200)
    national_highways_api_key: str | None = Field(None, max_length=200)
    # planespotters.net needs a contact (email or URL) in requests before it serves photos.
    contact: str | None = Field(None, max_length=200)

    @field_validator("nrod_username", "nrod_password", "opensky_client_id", "opensky_client_secret", "contact",
                     "readsb_url", "bods_api_key", "national_highways_api_key")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v

    @field_validator("train_watch")
    @classmethod
    def _watch(cls, v: str | None) -> str | None:
        if v is None:
            return v
        codes = _codes(v)
        if codes and (len(codes) != 2 or codes[0] == codes[1]):
            raise ValueError("choose two different stations, or none")
        return ",".join(codes)

    @field_validator("train_boards")
    @classmethod
    def _boards(cls, v: str | None) -> str | None:
        if v is None:
            return v
        codes = list(dict.fromkeys(_codes(v)))
        if len(codes) > 4:
            raise ValueError("at most 4 departure boards")
        return ",".join(codes)

    def changes(self) -> dict:
        return self.model_dump(exclude_none=True)

    def station_codes(self) -> set[str]:
        return {c for field in (self.train_watch, self.train_boards) if field for c in _codes(field)}


class Backup(BaseModel):
    """A settings backup file (see /api/settings/backup); the values are checked separately."""

    app: str
    format: int
    exported: str | None = None
    includes_logins: bool = False
    settings: dict


EDITABLE = list(SettingsUpdate.model_fields)
VISIBLE = [k for k in EDITABLE if k not in SECRET_KEYS]


def settings_payload(current: Settings, defaults: Settings, overridden: dict, trains_available: bool) -> dict:
    return {
        "values": {k: getattr(current, k) for k in VISIBLE},
        "defaults": {k: getattr(defaults, k) for k in VISIBLE},
        "overridden": sorted(k for k in overridden if k in EDITABLE),
        "trains_available": trains_available,
        # Whether each secret is set, never its value.
        "secrets": {
            "nrod_password": bool(current.nrod_password),
            "opensky_client_secret": bool(current.opensky_client_secret),
            "bods_api_key": bool(current.bods_api_key),
            "national_highways_api_key": bool(current.national_highways_api_key),
        },
    }
