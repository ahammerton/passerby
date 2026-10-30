"""FastAPI entry point: background polling, REST API, WebSocket push, static frontend.

All background work lives in a Runtime, which the settings page restarts in
place when settings change (connected browsers stay connected).
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from contextlib import asynccontextmanager, suppress

import httpx
from pydantic import BaseModel, Field, ValidationError
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .aggregator import Aggregator
from .aircraft_types import classify
from .airlines import ICAO_CODE, Logos, identify
from .interesting import RARE_WINDOW_DAYS, Interest
from .bus_timetable import FILE as BUS_TIMETABLE_FILE
from .bus_timetable import BusTimetable, area_key, build, download, pick_region, stale
from . import radar, roads as road_closures
from .roads import Roads
from .bus_disruptions import INTERVAL_S, Disruptions
from .buses import BusFeedError, BusTracker, check_bods_key
from .airports import Airport, load_airports, nearest_airports
from .config import Settings, env_settings, get_settings, read_overrides, reload_settings, write_overrides
from .credentials import check_nrod, check_opensky
from .enrichment import ICAO24, EnrichmentCache, Enricher
from .geo import haversine_km
from .security import COOKIE, SESSION_SECONDS, AdminAuth, SecurityMiddleware
from .sources import (
    ADSBLOL_URL, AIRPLANESLIVE_URL, AircraftSource, CommunitySource, OpenSkySource, ReadsbSource, find_receiver,
)
from .store import SightingStore
from .trains import (
    MARGIN_KM, ServiceInfo, TimetableFeed, TrainTracker, TrustFeed, load_corpus, load_railways, load_station_layout,
    load_stations, read_tiplocs, stations_within,
)
from .user_settings import (
    EDITABLE, NROD_KEYS, OPENSKY_KEYS, RECEIVER_KEYS, SECRET_KEYS, TRAIN_AREA_KEYS, VISIBLE, Backup, SettingsUpdate,
    settings_payload,
)
from .weather import fetch_metars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO, including signed download links; keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("passerby")


class AppState:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, clients: set[WebSocket]):
        self.settings = settings
        self.client = client
        self.sources = build_sources(settings, client)
        self.aggregator = Aggregator(
            home_lat=settings.home_lat,
            home_lon=settings.home_lon,
            radius_km=settings.radius_km,
            alert_radius_km=settings.alert_radius_km,
            alert_min_altitude_ft=settings.alert_min_altitude_ft,
            stale_after=settings.stale_after,
            priorities={s.name: s.priority for s in self.sources},
        )
        self.store = SightingStore(settings.data_dir / "sightings.db")
        self.enrichment_cache = EnrichmentCache(settings.data_dir / "enrichment.db")
        self.enricher = Enricher(
            client,
            self.enrichment_cache,
            delay=settings.enrichment_delay,
            photos_enabled=settings.photos_available,
        )
        self.logos = Logos(client, settings.data_dir / "logos")
        self.interest = Interest(settings.interesting_alerts)
        self.airports: list[Airport] = []
        self.weather: list[dict] = []
        self.source_status: dict[str, dict] = {
            s.name: {"ok": None, "last_update": None, "count": 0, "error": None, "interval": s.interval}
            for s in self.sources
        }
        # Shared with the Runtime, so browsers stay connected across restarts.
        self.clients = clients
        self.snapshot: list = []
        self.trains: TrainTracker | None = None
        self.train_state: dict | None = None
        self.buses: BusTracker | None = None
        self.roads: Roads | None = None


def build_sources(settings: Settings, client: httpx.AsyncClient) -> list[AircraftSource]:
    sources: list[AircraftSource] = []
    if settings.readsb_enabled:
        sources.append(ReadsbSource(client, url=settings.readsb_url, interval=settings.readsb_interval))
    community = [
        ("adsblol", ADSBLOL_URL, settings.adsblol_enabled, settings.adsblol_interval),
        ("airplaneslive", AIRPLANESLIVE_URL, settings.airplaneslive_enabled, settings.airplaneslive_interval),
    ]
    for name, url, enabled, interval in community:
        if enabled:
            sources.append(CommunitySource(
                client, name=name, base_url=url, lat=settings.home_lat, lon=settings.home_lon,
                radius_km=settings.radius_km, interval=interval,
            ))
    if settings.opensky_enabled:
        sources.append(OpenSkySource(
            client,
            lat=settings.home_lat,
            lon=settings.home_lon,
            radius_km=settings.radius_km,
            interval=settings.effective_opensky_interval,
            client_id=settings.opensky_client_id,
            client_secret=settings.opensky_client_secret,
        ))
    return sources


async def build_train_tracker(settings: Settings, client: httpx.AsyncClient) -> TrainTracker | None:
    try:
        stations = await load_stations(client, settings.data_dir)
        corpus = await load_corpus(client, settings.data_dir, settings.nrod_username, settings.nrod_password)
        tiplocs = read_tiplocs(settings.data_dir)
        home = (settings.home_lat, settings.home_lon)
        boards = settings.train_board_codes or [s.crs for s in stations_within(stations, *home, 50)[:1]]
        tracker = TrainTracker(
            corpus=corpus,
            stations=stations,
            home=(settings.home_lat, settings.home_lon),
            radius_km=settings.train_radius_km,
            watch=settings.train_watch_codes,
            alert_seconds=settings.train_alert_seconds,
            info=ServiceInfo(tiplocs, stations),
            boards=boards,
        )
    except Exception as exc:
        log.error("Trains disabled: %s", exc)
        return None
    log.info("Trains: %d stations within %s km, watching %s",
             len(tracker.area_stations), tracker.radius_km,
             "-".join(s.crs for s in tracker.watch) if tracker.watch else "nothing")
    if tracker.watch_warning:
        log.warning("Trains past home: %s", tracker.watch_warning)
    return tracker


async def load_rail_lines(state: AppState) -> None:
    """Fetch railway track in the background, retrying until the Overpass servers answer.

    Trains run on straight lines between stations until it arrives.
    """
    s, delay = state.settings, 60
    while True:
        try:
            rail = await load_railways(state.client, s.data_dir, s.home_lat, s.home_lon, s.train_radius_km)
        except Exception as exc:
            log.warning("Railway lines unavailable (retrying in %ss), using straight lines: %s", delay, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1800)
            continue
        state.trains.set_rail(rail)
        log.info("Railway lines: %d track sections", len(rail.lines))
        return


async def keep_timetable(state: AppState) -> None:
    """Load the timetable, then apply each night's update (checked hourly; downloads only when due)."""
    s, trains = state.settings, state.trains
    # Same area as the railway lines: the train radius plus a margin for trains entering it.
    area_crs = {st.crs for st in stations_within(trains.stations, *trains.home, s.train_radius_km + MARGIN_KM)}
    area = {t for crs in area_crs for t in trains.info.by_crs.get(crs, ())}
    feed = TimetableFeed(state.client, s.nrod_username, s.nrod_password, s.data_dir, area)
    delay = 300
    while True:
        try:
            before = trains.timetable.sequence if trains.timetable else None
            timetable = await feed.refresh()
            if timetable.sequence != before:
                trains.set_timetable(timetable)
                log.info("Timetable ready: %d schedules (sequence %s)", len(timetable.schedules), timetable.sequence)
            delay = 300
            await asyncio.sleep(3600)
        except Exception as exc:
            log.warning("Timetable update failed (retrying in %ss): %s", delay, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 6 * 3600)


# A receiver that has answered within this long counts as working.
RECEIVER_HEALTHY_S = 30
# In backup mode, online feeds give a receiver this long to answer for the first time.
RECEIVER_FIRST_ANSWER_S = 10


def receiver_working(state: AppState) -> bool:
    now = time.time()
    return any(
        s.local and state.source_status[s.name]["ok"]
        and now - (state.source_status[s.name]["last_update"] or 0) < RECEIVER_HEALTHY_S
        for s in state.sources
    )


def receiver_not_tried_yet(state: AppState) -> bool:
    return any(s.local and state.source_status[s.name]["ok"] is None for s in state.sources)


async def poll_source(state: AppState, source: AircraftSource) -> None:
    status = state.source_status[source.name]
    backoff = 0.0
    backup = not source.local and state.settings.online_feeds == "backup"
    if backup:
        # Don't fetch online just because the receiver hasn't had its first turn yet.
        waited = 0
        while receiver_not_tried_yet(state) and waited < RECEIVER_FIRST_ANSWER_S:
            await asyncio.sleep(1)
            waited += 1
    while True:
        if backup and receiver_working(state):
            # Backup mode: online feeds stand by (and save their request quotas) while the receiver works.
            if not status.get("standby"):
                log.info("%s standing by: the local receiver is working", source.name)
                state.aggregator.update(source.name, [])
            status.update(standby=True, count=0)
            await asyncio.sleep(5)
            continue
        if status.get("standby"):
            log.info("%s resuming: the local receiver isn't answering", source.name)
            status["standby"] = False
        try:
            aircraft = await source.fetch()
            state.aggregator.update(source.name, aircraft)
            status.update(ok=True, last_update=time.time(), count=len(aircraft), error=None)
            backoff = 0.0
        except Exception as exc:  # keep polling whatever goes wrong
            log.warning("%s fetch failed: %s", source.name, exc)
            status.update(ok=False, error=str(exc))
            # Rate limited: back off progressively (up to 5 min) instead of retrying at full speed.
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                backoff = min(max(backoff * 2, source.interval), 300)
        await asyncio.sleep(source.interval + backoff)


async def poll_buses(state: AppState) -> None:
    buses, backoff = state.buses, 0.0
    while True:
        try:
            await buses.poll_once()
            backoff = 0.0
        except BusFeedError as exc:  # never includes the URL, which carries the API key
            log.warning("Buses: %s", exc)
            buses.status.update(ok=False, error=str(exc))
            backoff = min(max(backoff * 2, buses.interval), 300)
        except Exception as exc:
            log.warning("Buses: unexpected %s", type(exc).__name__)
            buses.status.update(ok=False, error=f"unexpected {type(exc).__name__}")
            backoff = min(max(backoff * 2, buses.interval), 300)
        await asyncio.sleep(buses.interval + backoff)


async def poll_bus_disruptions(state: AppState) -> None:
    buses = state.buses
    feed = buses.disruptions
    while True:
        try:
            await feed.fetch()
            feed.refresh(buses.routes())
            delay = INTERVAL_S
        except BusFeedError as exc:  # never includes the URL, which carries the API key
            log.warning("Bus disruptions: %s", exc)
            feed.status.update(ok=False, error=str(exc))
            delay = 120
        except Exception as exc:
            log.warning("Bus disruptions: unexpected %s", type(exc).__name__)
            feed.status.update(ok=False, error=f"unexpected {type(exc).__name__}")
            delay = 120
        await asyncio.sleep(delay)


async def keep_bus_timetable(state: AppState) -> None:
    """Load the area's bus timetable, refreshing it weekly (a ~300 MB regional download)."""
    s, buses = state.settings, state.buses
    region = pick_region(s.home_lat, s.home_lon) if s.bus_region == "auto" else s.bus_region
    # Trips through the area buses are fetched for (plus a margin, added when it's built).
    area = area_key(s.home_lat, s.home_lon, s.bus_radius_km)
    path = s.data_dir / BUS_TIMETABLE_FILE
    status = buses.status["timetable"] = {"region": region, "state": "loading", "trips": 0, "error": None}
    table = await asyncio.to_thread(BusTimetable.load, path)
    if table:
        # Even one built for a different area or radius is used until its replacement is ready:
        # most journeys still match, so routes, delays and next stops don't vanish for the
        # minute or so a rebuild takes.
        buses.timetable = table
        status.update(state="ready", trips=len(table.trips), version=table.version, built=table.built)
    delay = 600
    while True:
        if stale(buses.timetable, region, area):
            zip_path = s.data_dir / "bus_gtfs.zip"
            try:
                log.info("Bus timetable: downloading the %s region from BODS", region)
                status.update(state="downloading", error=None)
                await download(state.client, region, zip_path)
                status.update(state="processing")
                data = await asyncio.to_thread(build, zip_path, s.home_lat, s.home_lon, s.bus_radius_km)
                built = time.time()
                await asyncio.to_thread(BusTimetable.save, path, data, region, area, built)
                buses.timetable = BusTimetable(data, built, region, area)
                status.update(state="ready", trips=len(data["trips"]), version=data["version"], built=built)
                log.info("Bus timetable ready: %d trips through the area (%s)", len(data["trips"]), data["version"])
                delay = 600
            except Exception as exc:
                log.warning("Bus timetable update failed (retrying in %ss): %s", delay, exc)
                status.update(state="error" if not buses.timetable else "ready", error=str(exc))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 6 * 3600)
                continue
            finally:
                zip_path.unlink(missing_ok=True)
                zip_path.with_suffix(".part").unlink(missing_ok=True)
        await asyncio.sleep(6 * 3600)


async def poll_weather(state: AppState) -> None:
    s = state.settings
    while True:
        try:
            # Weather only matters close to home; cap the search at 100 km.
            state.weather = await fetch_metars(state.client, s.home_lat, s.home_lon, min(s.radius_km, 100))
        except Exception as exc:
            log.warning("weather fetch failed: %s", exc)
        await asyncio.sleep(s.weather_interval)


async def broadcast_loop(state: AppState) -> None:
    while True:
        snapshot = state.aggregator.snapshot()
        if state.settings.enrichment_enabled:
            snapshot = state.enricher.annotate(snapshot)
        # After enrichment, which can fill in the type code.
        snapshot = [a.model_copy(update=classify(a) | identify(a.callsign, a.military, a.route and a.route.airline))
                    for a in snapshot]
        if state.interest.due():
            since = time.time() - RARE_WINDOW_DAYS * 86400
            state.interest.set_history(*await asyncio.to_thread(state.store.type_history, since))
        snapshot = state.interest.annotate(snapshot)
        state.snapshot = snapshot
        alerts = state.aggregator.new_overhead(snapshot)
        interest_alerts = state.interest.new_alerts(snapshot)
        if snapshot:
            await asyncio.to_thread(state.store.record, snapshot)
        message = {
            "type": "snapshot",
            "time": time.time(),
            "aircraft": [a.model_dump() for a in snapshot],
            "sources": state.source_status,
        }
        alert_messages = [{"type": "alert", "aircraft": a.model_dump()} for a in alerts]
        alert_messages += [{"type": "interest_alert", "aircraft": a.model_dump()} for a in interest_alerts]
        if state.trains:
            state.train_state, train_alerts = state.trains.snapshot()
            message["trains"] = state.train_state
            alert_messages += [{"type": "train_alert", "train": t} for t in train_alerts]
        if state.buses:
            message["buses"] = state.buses.snapshot()
        if state.roads:
            message["roads"] = state.roads.snapshot()
        for ws in list(state.clients):
            try:
                await ws.send_json(message)
                for alert in alert_messages:
                    await ws.send_json(alert)
            except Exception:
                state.clients.discard(ws)
        await asyncio.sleep(state.settings.broadcast_interval)


class Runtime:
    """Owns the HTTP client, the app state and every background task."""

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.state: AppState | None = None
        self.client: httpx.AsyncClient | None = None
        self.tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    async def start(self, settings: Settings) -> None:
        client = self.client = httpx.AsyncClient(timeout=20, headers={"User-Agent": settings.user_agent})
        state = AppState(settings, client, self.clients)
        try:
            state.airports = await load_airports(client, settings.data_dir)
            log.info("Loaded %d airports", len(state.airports))
        except Exception as exc:
            log.warning("Airport data unavailable: %s", exc)

        if not state.sources:
            log.warning("No aircraft sources enabled — check OPENSKY_ENABLED / READSB_ENABLED")
        for s in state.sources:
            log.info("Source %s every %ss", s.name, s.interval)

        if settings.nrod_username and settings.nrod_password:
            state.trains = await build_train_tracker(settings, client)

        tasks = [asyncio.create_task(poll_source(state, s)) for s in state.sources]
        if state.trains:
            feed = TrustFeed(settings.nrod_username, settings.nrod_password,
                             state.trains.ingest, state.trains.status)
            tasks.append(asyncio.create_task(feed.run()))
            tasks.append(asyncio.create_task(load_rail_lines(state)))
            tasks.append(asyncio.create_task(keep_timetable(state)))
        if settings.buses_available:
            state.buses = BusTracker(client, settings.bods_api_key, settings.home_lat, settings.home_lon,
                                     settings.bus_radius_km, settings.bus_interval, settings.data_dir)
            tasks.append(asyncio.create_task(poll_buses(state)))
            if settings.bus_delays:
                tasks.append(asyncio.create_task(keep_bus_timetable(state)))
            if settings.bus_disruptions:
                state.buses.disruptions = Disruptions(client, settings.bods_api_key, settings.home_lat,
                                                      settings.home_lon, settings.bus_radius_km)
                tasks.append(asyncio.create_task(poll_bus_disruptions(state)))
            log.info("Buses within %s km every %ss", settings.bus_radius_km, settings.bus_interval)
        if settings.roads_available:
            state.roads = Roads(client, settings.national_highways_api_key, settings.home_lat, settings.home_lon,
                                settings.road_radius_km)
            tasks.append(asyncio.create_task(road_closures.run(state.roads)))
            log.info("Road closures within %s km", settings.road_radius_km)
        tasks.append(asyncio.create_task(poll_weather(state)))
        if settings.enrichment_enabled:
            tasks.append(asyncio.create_task(state.enricher.run()))
        if settings.photos_enabled and not settings.contact:
            log.info("Aircraft photos disabled: set CONTACT to enable planespotters.net photos")
        tasks.append(asyncio.create_task(broadcast_loop(state)))
        self.state, self.tasks = state, tasks

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with suppress(asyncio.CancelledError):
                await t
        self.tasks = []
        if self.state:
            self.state.store.close()
            self.state.enrichment_cache.close()
        if self.client:
            await self.client.aclose()

    async def restart(self) -> None:
        """Apply changed settings: stop everything, then start again with the new values."""
        async with self._lock:
            log.info("Restarting with updated settings")
            await self.stop()
            await self.start(reload_settings())


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Passerby %s", __version__)
    runtime = app.state.runtime = Runtime()
    await runtime.start(get_settings())
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="Passerby", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Both come from .env only (the settings page can't change them), so they're fixed at startup.
_startup = get_settings()
app.add_middleware(SecurityMiddleware, allowed_hosts=_startup.allowed_hosts.split(","))
admin = AdminAuth(_startup.admin_password, _startup.data_dir)
if not admin.enabled:
    log.warning("No ADMIN_PASSWORD set: anyone who can reach this app can change its settings")


def require_admin(request: Request) -> None:
    admin.require(request)


def tracker() -> AppState:
    return app.state.runtime.state


@app.get("/api/config")
def config():
    s = tracker().settings
    return {
        "home": {"lat": s.home_lat, "lon": s.home_lon, "name": s.home_name},
        "radius_km": s.radius_km,
        "alert_radius_km": s.alert_radius_km,
        "sources": [src.name for src in tracker().sources],
        "photos": s.photos_available,
        "trains": tracker().trains is not None,
        "train_radius_km": s.train_radius_km,
        "passing_window": {"enabled": s.passing_window, "minutes": s.passing_minutes},
        "buses": {"enabled": tracker().buses is not None, "min_zoom": s.bus_min_zoom},
        "roads": {"enabled": tracker().roads is not None},
        "units": {"distance": s.distance_unit, "altitude": s.altitude_unit, "speed": s.speed_unit},
        "aircraft_colours": s.aircraft_colours,
        "version": __version__,
    }


# ---------- signing in (only when ADMIN_PASSWORD is set) ----------

class Login(BaseModel):
    password: str = Field(max_length=200)


@app.get("/api/session")
def session(request: Request):
    return {"required": admin.enabled, "signed_in": admin.valid(request.cookies.get(COOKIE))}


@app.post("/api/login")
async def login(body: Login, request: Request, response: Response):
    if not admin.enabled:
        return {"signed_in": True}
    client = request.client.host if request.client else "?"
    if not await admin.check_password(body.password, client):
        raise HTTPException(401, "Wrong password.")
    response.set_cookie(COOKIE, admin.new_session(), max_age=SESSION_SECONDS, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")
    return {"signed_in": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"signed_in": False}


# ---------- settings page ----------

def _settings_response(**extra) -> dict:
    s = get_settings()
    trains_available = bool(s.nrod_username and s.nrod_password)
    return settings_payload(s, env_settings(), read_overrides(s.data_dir), trains_available) | {
        # Backups can only include logins when there's a password protecting them.
        "admin_password_set": admin.enabled} | extra


@app.get("/api/settings", dependencies=[Depends(require_admin)])
def get_user_settings():
    return _settings_response()


async def _station_list() -> dict:
    s = get_settings()
    return await load_stations(app.state.runtime.client, s.data_dir)


@app.put("/api/settings", dependencies=[Depends(require_admin)])
async def put_user_settings(update: SettingsUpdate):
    changes = update.changes()
    if not changes:
        return _settings_response(restarted=False)
    codes = update.station_codes()
    if codes:
        unknown = sorted(codes - set(await _station_list()))
        if unknown:
            raise HTTPException(422, f"Unknown station code(s): {', '.join(unknown)}")
    s = get_settings()
    before = {k: getattr(s, k) for k in changes}
    # Check changed logins before saving them, so a typo can't break a feed.
    client = app.state.runtime.client
    if changes.keys() & NROD_KEYS:
        user, password = changes.get("nrod_username", s.nrod_username), changes.get("nrod_password", s.nrod_password)
        if user and password and (problem := await check_nrod(client, user, password)):
            raise HTTPException(422, problem)
    if changes.keys() & OPENSKY_KEYS:
        cid = changes.get("opensky_client_id", s.opensky_client_id)
        secret = changes.get("opensky_client_secret", s.opensky_client_secret)
        if cid and secret and (problem := await check_opensky(client, cid, secret)):
            raise HTTPException(422, problem)
    if changes.get("bods_api_key") and (problem := await check_bods_key(client, changes["bods_api_key"])):
        raise HTTPException(422, problem)
    if changes.get("national_highways_api_key") and (
            problem := await road_closures.check_key(client, changes["national_highways_api_key"])):
        raise HTTPException(422, problem)
    # A receiver typed as just an IP or host: find its aircraft.json and store that.
    enabling = changes.get("readsb_enabled", s.readsb_enabled)
    if enabling and changes.keys() & RECEIVER_KEYS:
        address = changes.get("readsb_url", s.readsb_url)
        found = await find_receiver(client, address)
        if not found:
            raise HTTPException(422, f"No ADS-B receiver answered at {address}. Check the address, "
                                     "or enter the full URL of its aircraft.json.")
        changes["readsb_url"] = found[0]
        before.setdefault("readsb_url", s.readsb_url)
    overrides = read_overrides(s.data_dir) | changes
    # Values that match the .env defaults needn't be stored.
    defaults = env_settings()
    overrides = {k: v for k, v in overrides.items() if getattr(defaults, k, None) != v}
    write_overrides(s.data_dir, overrides)
    changed = {k for k, v in changes.items() if before[k] != v}
    notes = []
    if changed & TRAIN_AREA_KEYS and s.nrod_username:
        notes.append("Train area changed: railway lines and the timetable are being downloaded "
                     "again for the new area, which can take a minute or two.")
    if changed:
        await app.state.runtime.restart()
        trains = tracker().trains
        if trains and trains.watch_warning:
            notes.append(trains.watch_warning)
    return _settings_response(restarted=bool(changed), notes=notes)


@app.delete("/api/settings", dependencies=[Depends(require_admin)])
async def reset_user_settings():
    """Forget everything saved from the settings page and go back to .env."""
    s = get_settings()
    had = read_overrides(s.data_dir)
    write_overrides(s.data_dir, {})
    if had:
        await app.state.runtime.restart()
    return _settings_response(restarted=bool(had), notes=[])


# ---------- backup and restore ----------

BACKUP_APP = "passerby"
BACKUP_OLD_APPS = {"transport-tracker"}  # backups made before the rename
BACKUP_FORMAT = 1


@app.get("/api/settings/backup", dependencies=[Depends(require_admin)])
def backup_settings(secrets: bool = False):
    """Every setting from the settings page as a file; logins only on request, behind a password."""
    if secrets and not admin.enabled:
        raise HTTPException(403, "Set ADMIN_PASSWORD in .env before including logins in a backup: "
                                 "without it, anyone on the network could download them.")
    s = get_settings()
    values = {k: getattr(s, k) for k in (EDITABLE if secrets else VISIBLE)}
    body = {"app": BACKUP_APP, "format": BACKUP_FORMAT,
            "exported": datetime.now().astimezone().isoformat(timespec="seconds"),
            "includes_logins": secrets, "settings": values}
    name = f"passerby-settings-{datetime.now():%Y%m%d}.json"
    return Response(json.dumps(body, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.post("/api/settings/restore", dependencies=[Depends(require_admin)])
async def restore_settings(backup: Backup):
    """Replace the settings-page values with a backup's (logins it doesn't have are kept)."""
    if backup.app not in {BACKUP_APP, *BACKUP_OLD_APPS} or backup.format != BACKUP_FORMAT:
        raise HTTPException(422, "That isn't a Passerby settings backup.")
    known = {k: v for k, v in backup.settings.items() if k in EDITABLE}
    ignored = sorted(set(backup.settings) - set(known))
    try:
        update = SettingsUpdate(**known)
    except ValidationError as exc:
        problems = "; ".join(f"{e['loc'][-1]}: {e['msg']}" for e in exc.errors())
        raise HTTPException(422, f"The backup has values this version can't use: {problems}") from None
    codes = update.station_codes()
    if codes and (unknown := sorted(codes - set(await _station_list()))):
        raise HTTPException(422, f"Unknown station code(s) in the backup: {', '.join(unknown)}")
    s = get_settings()
    restored = update.changes()
    kept = [k for k in sorted(SECRET_KEYS) if k not in restored and getattr(s, k)]
    for k in kept:
        restored[k] = getattr(s, k)
    defaults = env_settings()
    write_overrides(s.data_dir, {k: v for k, v in restored.items() if getattr(defaults, k, None) != v})
    await app.state.runtime.restart()
    notes = []
    if kept:
        notes.append("The backup had no passwords or keys, so the current ones were kept.")
    if ignored:
        notes.append(f"Ignored settings this version doesn't have: {', '.join(ignored)}.")
    return _settings_response(restarted=True, notes=notes)


class ReceiverQuery(BaseModel):
    address: str = Field(min_length=1, max_length=300)


@app.post("/api/receiver", dependencies=[Depends(require_admin)])
async def test_receiver(query: ReceiverQuery):
    """Look for an ADS-B receiver's aircraft.json at an IP, host or URL (settings page "Find")."""
    address = query.address.strip()
    found = await find_receiver(app.state.runtime.client, address)
    if not found:
        raise HTTPException(404, f"No ADS-B receiver answered at {address}.")
    url, total, positioned = found
    return {"url": url, "aircraft": total, "with_position": positioned}


@app.get("/api/stations")
async def nearby_stations(lat: float, lon: float, radius_km: float = 25, limit: int = 40):
    """Stations near a point, for the settings page's station pickers."""
    stations = await _station_list()
    near = stations_within(stations, lat, lon, min(radius_km, 100))[:min(limit, 200)]
    return [{**st.as_dict(), "distance_km": round(haversine_km(lat, lon, st.lat, st.lon), 1)} for st in near]


@app.get("/api/aircraft")
def aircraft():
    return [a.model_dump() for a in tracker().snapshot]


@app.get("/api/overhead")
def overhead():
    return [a.model_dump() for a in tracker().snapshot if a.overhead]


@app.get("/api/status")
def status():
    return tracker().source_status


@app.get("/api/airports")
def airports(limit: int = 15):
    s = tracker().settings
    return nearest_airports(tracker().airports, s.home_lat, s.home_lon, s.radius_km, limit)


@app.get("/api/weather")
def weather():
    return tracker().weather


@app.get("/api/trains")
def trains():
    state = tracker()
    if state.trains is None:
        raise HTTPException(404, "Trains are not enabled (set NROD_USERNAME and NROD_PASSWORD)")
    # Before the first broadcast tick there is nothing computed yet.
    return state.train_state or {"status": state.trains.status, "positions": [], "watch": None}


# A station whose layout couldn't be downloaded isn't tried again for this long.
LAYOUT_RETRY_S = 600
_layout_failures: dict[str, tuple[float, str]] = {}
# Layout downloads in progress, so the station view refreshing (or two browsers) while
# OpenStreetMap is slow wait for the same download rather than each starting another.
_layout_loads: dict[str, asyncio.Task] = {}


async def _station_layout(st) -> dict:
    task = _layout_loads.get(st.crs)
    if task is None:
        task = asyncio.create_task(load_station_layout(app.state.runtime.client, get_settings().data_dir, st))
        _layout_loads[st.crs] = task

        def done(t: asyncio.Task, crs=st.crs) -> None:
            _layout_loads.pop(crs, None)
            if not t.cancelled():
                t.exception()  # seen, even if every browser waiting for it has gone

        task.add_done_callback(done)
    # A browser giving up doesn't cancel the download: the layout is still saved for next time.
    return await asyncio.shield(task)


@app.get("/api/station/{crs}")
async def station(crs: str):
    """One station: its layout (OpenStreetMap), the trains there with their platforms, and departures."""
    trains = tracker().trains
    crs = crs.upper()
    if trains is None:
        raise HTTPException(404, "Trains are not enabled")
    st = trains.stations.get(crs)
    if st is None:
        raise HTTPException(404, f"Unknown station {crs}")
    failed = _layout_failures.get(crs)
    if failed and time.time() - failed[0] < LAYOUT_RETRY_S:
        layout, layout_error = None, failed[1]  # don't ask OpenStreetMap again on every refresh
    else:
        try:
            layout, layout_error = await _station_layout(st), None
            _layout_failures.pop(crs, None)
        except Exception as exc:
            layout, layout_error = None, str(exc)
            _layout_failures[crs] = (time.time(), layout_error)
    return {"station": st.as_dict(), "layout": layout, "layout_error": layout_error, **trains.station_view(crs)}


@app.get("/api/buses")
def buses():
    state = tracker()
    if state.buses is None:
        raise HTTPException(404, "Buses are not enabled (set BODS_API_KEY)")
    return state.buses.snapshot()


@app.get("/api/buses/route")
def bus_route(id: str):
    """The route a bus is running: its road path (or its stops joined up) and every stop."""
    state = tracker()
    if state.buses is None:
        raise HTTPException(404, "Buses are not enabled (set BODS_API_KEY)")
    bus = next((b for b in state.buses.buses if b["id"] == id), None)
    if bus is None:
        raise HTTPException(404, "That bus isn't reporting nearby any more")
    route = state.buses.timetable.route(bus) if state.buses.timetable else None
    if route is None:
        raise HTTPException(404, "That bus couldn't be matched to a timetable")
    return route


@app.get("/api/roads")
def roads():
    state = tracker()
    if state.roads is None:
        raise HTTPException(404, "Roads are not enabled (set NATIONAL_HIGHWAYS_API_KEY)")
    return state.roads.snapshot()


@app.get("/api/radar")
async def rain_radar():
    """The last two hours of rain radar, as map tile addresses (RainViewer)."""
    try:
        return {"frames": await radar.frames(app.state.runtime.client), "max_zoom": radar.MAX_ZOOM}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Couldn't get the rain radar: {type(exc).__name__}") from None


@app.get("/api/railways")
def railways():
    """Railway lines around home (OpenStreetMap), drawn once by the browser."""
    trains = tracker().trains
    if trains is None or trains.rail is None:
        raise HTTPException(404, "No railway data")
    return trains.rail.as_dict()


@app.get("/api/photo/{icao24}")
async def photo(icao24: str):
    icao24 = icao24.lower()
    if not ICAO24.match(icao24):
        raise HTTPException(400, "icao24 must be 6 hex characters")
    try:
        return await tracker().enricher.photo(icao24)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"photo lookup failed: {exc}") from exc


@app.get("/api/logo/{code}")
async def logo(code: str):
    code = code.upper()
    if not ICAO_CODE.match(code):
        raise HTTPException(400, "code must be a 3-letter ICAO airline code")
    try:
        path = await tracker().logos.get(code)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"logo lookup failed: {exc}") from exc
    if path is None:
        raise HTTPException(404, "No logo for this airline")
    return FileResponse(path, media_type="image/png", headers={"cache-control": "public, max-age=604800"})


@app.get("/api/history")
def history(limit: int = 100, overhead_only: bool = False):
    return tracker().store.recent(min(limit, 1000), overhead_only)


@app.get("/api/stats")
def stats():
    store = tracker().store
    now = time.time()
    return {"last_24h": store.stats(now - 86400), "last_7d": store.stats(now - 7 * 86400)}


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    state = tracker()
    state.clients.add(ws)
    try:
        # Send what we have straight away rather than waiting for the next tick.
        first = {
            "type": "snapshot",
            "time": time.time(),
            "aircraft": [a.model_dump() for a in state.snapshot],
            "sources": state.source_status,
        }
        if state.train_state:
            first["trains"] = state.train_state
        if state.buses:
            first["buses"] = state.buses.snapshot()
        if state.roads:
            first["roads"] = state.roads.snapshot()
        await ws.send_json(first)
        while True:
            await ws.receive_text()  # clients don't send anything; this detects disconnects
    except WebSocketDisconnect:
        pass
    finally:
        state.clients.discard(ws)


# Mounted last so /api and /ws take precedence.
class Frontend(StaticFiles):
    """The app's pages and scripts. Browsers check for a newer copy on every load (a quick
    "not modified" when there isn't one), so an update shows without a forced reload."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = "no-cache"
        return response


app.mount("/", Frontend(directory=get_settings().frontend_dir, html=True), name="frontend")
