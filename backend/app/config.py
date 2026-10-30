"""Runtime settings: environment variables / .env (see .env.example), overlaid by
changes saved from the settings page (data/settings.json)."""

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from . import __version__

log = logging.getLogger(__name__)

OVERRIDES_FILE = "settings.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Home location — the centre of everything the app shows.
    home_lat: float = 51.4700
    home_lon: float = -0.4543
    home_name: str = "Heathrow Airport"

    # Aircraft further than this from home are ignored.
    radius_km: float = 150.0
    # Display units in the browser (everything is stored in km, ft and knots).
    distance_unit: Literal["km", "mi", "nm"] = "km"
    altitude_unit: Literal["ft", "m"] = "ft"
    speed_unit: Literal["kt", "mph", "kmh"] = "kt"
    # Aircraft icons in their airline's colours (others by altitude), or all by altitude.
    aircraft_colours: Literal["airline", "altitude"] = "airline"

    # Aircraft inside this radius count as "overhead" and trigger alerts.
    alert_radius_km: float = 10.0
    # Aircraft below this (taxiing, on the runway) never count as overhead.
    alert_min_altitude_ft: float = 300.0

    # OpenSky Network (https://opensky-network.org). Leave the client
    # credentials empty to use anonymous access (much lower daily quota).
    opensky_enabled: bool = True
    opensky_client_id: str = ""
    opensky_client_secret: str = ""
    # Seconds between polls. 0 = pick a safe default for the quota.
    opensky_interval: int = 0

    # Community ADS-B networks (unfiltered, includes military). adsb.lol is
    # open; airplanes.live asks you to email them before using the API.
    adsblol_enabled: bool = True
    # adsb.lol rate-limits per IP address; every 30 s stays well clear of it (and planes are
    # animated smoothly in between).
    adsblol_interval: int = 30
    airplaneslive_enabled: bool = False
    airplaneslive_interval: int = 10

    # Your own ADS-B receiver (readsb, tar1090, dump1090, a Flightradar24 or FlightAware
    # feeder...). Points at its aircraft.json; the settings page can find it from just an IP.
    readsb_enabled: bool = False
    readsb_url: str = "http://localhost:8080/data/aircraft.json"
    readsb_interval: int = 2
    # With a receiver: "fill" = online feeds add aircraft the receiver can't see (the receiver
    # wins for any aircraft both have); "backup" = online feeds only run while it's down.
    online_feeds: Literal["fill", "backup"] = "fill"

    # Aircraft details and routes from adsbdb.com (seconds between lookups).
    enrichment_enabled: bool = True
    enrichment_delay: float = 1.0
    # Photos from planespotters.net. They require a contact URL or email in
    # the User-Agent, so photos stay off until CONTACT is set.
    photos_enabled: bool = True
    contact: str = ""

    # UK trains from Network Rail's open data feeds (TRUST train movements).
    # Register free at https://publicdatafeeds.networkrail.co.uk. Trains are
    # off until a login is set.
    nrod_username: str = ""
    nrod_password: str = ""
    # Every train within this radius of home is shown.
    train_radius_km: float = 5.0
    # Two adjacent stations either side of home ("CLJ,WAT") for "trains past home".
    train_watch: str = ""
    # Departure boards for these station codes (comma separated). Empty = the station nearest home.
    train_boards: str = ""
    # Alert this many seconds before a train is estimated to pass home.
    train_alert_seconds: int = 60
    # The small "Passing home" window on the map: on/off, and how far ahead it looks.
    passing_window: bool = True
    passing_minutes: int = 5

    # Live buses from the Bus Open Data Service (England). Free API key from
    # https://data.bus-data.dft.gov.uk; buses stay off until it's set.
    bods_api_key: str = ""
    buses_enabled: bool = True
    # Buses within this distance of home are fetched...
    bus_radius_km: float = 10.0
    # ...and shown only when the map is zoomed in at least this far (12 is a town and its
    # surroundings, 13 a town).
    bus_min_zoom: int = 12
    # Seconds between polls (BODS itself refreshes about every 10 s).
    bus_interval: int = 15
    # Delays and next stops, from BODS's timetables (a ~300 MB weekly download per region).
    bus_delays: bool = True
    # Diversions, stop closures and other disruptions near home (BODS SIRI-SX, every 10 min).
    bus_disruptions: bool = True
    # The BODS timetable region, or "auto" to pick the one nearest home.
    bus_region: Literal["auto", "south_east", "london", "south_west", "east_anglia", "east_midlands",
                        "west_midlands", "north_west", "north_east", "yorkshire"] = "auto"

    # Road closures and incidents on motorways and major A-roads, from National Highways.
    # Free key: https://developer.data.nationalhighways.co.uk ("Road & Lane Closures");
    # roads stay off until it's set.
    national_highways_api_key: str = ""
    roads_enabled: bool = True
    # Closures within this distance of home are shown.
    road_radius_km: float = 15.0

    # METAR weather for nearby airports from aviationweather.gov.
    weather_interval: int = 600

    # Seconds after which an aircraft with no updates is dropped.
    stale_after: int = 90
    # Seconds between pushes to connected browsers.
    broadcast_interval: float = 2.0

    # Password for the settings page (empty = anyone on the network can change settings).
    # Deliberately not editable from the settings page, so a typo there can't lock you out.
    admin_password: str = ""
    # Host names the app may be reached by, besides IP addresses, localhost and local names
    # (.local, .lan, .home.arpa, .internal). Comma separated; "*" allows any.
    allowed_hosts: str = ""

    data_dir: Path = Path("data")
    frontend_dir: Path = Path(__file__).resolve().parents[2] / "frontend"

    @property
    def opensky_authenticated(self) -> bool:
        return bool(self.opensky_client_id and self.opensky_client_secret)

    @property
    def train_board_codes(self) -> list[str]:
        return [c.strip().upper() for c in self.train_boards.split(",") if c.strip()]

    @property
    def train_watch_codes(self) -> tuple[str, str] | None:
        codes = [c.strip().upper() for c in self.train_watch.split(",") if c.strip()]
        if not codes:
            return None
        if len(codes) != 2:
            raise ValueError("TRAIN_WATCH must be two station codes, e.g. CLJ,WAT")
        return codes[0], codes[1]

    @property
    def user_agent(self) -> str:
        base = f"passerby/{__version__}"
        return f"{base} (+{self.contact})" if self.contact else base

    @property
    def buses_available(self) -> bool:
        return self.buses_enabled and bool(self.bods_api_key)

    @property
    def roads_available(self) -> bool:
        return self.roads_enabled and bool(self.national_highways_api_key)

    @property
    def photos_available(self) -> bool:
        return self.photos_enabled and bool(self.contact)

    @property
    def effective_opensky_interval(self) -> int:
        if self.opensky_interval > 0:
            return self.opensky_interval
        # A small bounding box costs 1 credit per call. Anonymous users get
        # ~400 credits/day, registered users ~4000/day — stay well under both.
        return 30 if self.opensky_authenticated else 240


def read_overrides(data_dir: Path) -> dict:
    """Settings saved from the settings page; they take precedence over the environment."""
    path = data_dir / OVERRIDES_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable %s: %s", path, exc)
        return {}


def write_overrides(data_dir: Path, values: dict) -> None:
    path = data_dir / OVERRIDES_FILE
    if not values:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    # Created owner-only from the start: it can hold passwords.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(values, indent=2, sort_keys=True))
    tmp.replace(path)


@lru_cache
def env_settings() -> Settings:
    """Settings from the environment / .env alone (the defaults the settings page resets to)."""
    return Settings()


@lru_cache
def get_settings() -> Settings:
    base = env_settings()
    overrides = read_overrides(base.data_dir)
    # Keyword arguments beat environment variables in pydantic-settings.
    return Settings(**overrides) if overrides else base


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
