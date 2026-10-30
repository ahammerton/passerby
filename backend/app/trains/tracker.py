"""Live trains around home from Network Rail TRUST movement reports.

TRUST reports a train arriving at / departing from timing points (STANOX
codes), with the next timing point and the expected minutes to reach it.
Only timing points that are stations have coordinates, so each train is
placed:
- at a station after arriving there,
- along the railway track (from OpenStreetMap, see railways.py) from the last
  station it reported at to the next station it is due at, while travelling;
  on a straight line if that stretch isn't in the track data. Reports at
  junctions in between only refine the timing,
- at the next station if it is overdue there (until it reports or goes stale).

Once the timetable (timetable.py) is loaded, each train is matched to its
schedule, from TRUST's activation message or, failing that, by headcode,
location and time. That adds origin, destination, operator and calling
points, and drives the departure boards.

Messages arrive on the STOMP thread; ingest() and snapshot() share a lock.
"""

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..geo import haversine_km
from .corpus import Location
from .passes import HomeLine
from .railways import RailNetwork, fraction_along, point_along
from .services import ServiceInfo
from .stations import Station, stations_within
from .timetable import UK, Schedule, Timetable
from .trust import (
    ACTIVATION, CANCELLATION, MOVEMENT, Movement, headcode, parse_activation, parse_movement, train_class,
)

# Drop trains not heard from in this long (they've left the network or the area).
STALE_SECONDS = 30 * 60
# Keep showing a train this long after it is due somewhere it hasn't reported at.
OVERDUE_SECONDS = 10 * 60
# Keep a terminated train on the map briefly.
TERMINATED_SECONDS = 120
# Ignore reports this far from now (corrections, test messages, clock errors).
MAX_REPORT_AGE = 2 * 3600
# The station view counts a train as arriving this long before it's due.
ARRIVING_SECONDS = 300
# "Approaching home" looks this far ahead in the timetable.
PASS_HORIZON = 3600
# Activations arrive for every train in the country; forget them after a day.
ACTIVATION_MAX_AGE = 26 * 3600


def project_fraction(a: Station, b: Station, lat: float, lon: float) -> float:
    """How far along the straight A→B line the closest point to (lat, lon) is, 0–1."""
    # An equirectangular projection is plenty accurate over a few kilometres.
    k = math.cos(math.radians((a.lat + b.lat) / 2))
    bx, by = (b.lon - a.lon) * k, b.lat - a.lat
    px, py = (lon - a.lon) * k, lat - a.lat
    length_sq = bx * bx + by * by
    if length_sq == 0:
        return 0.0
    return min(max((px * bx + py * by) / length_sq, 0.0), 1.0)


@dataclass
class TrainState:
    train_id: str
    last: Movement
    # Last report at a location with coordinates: (station, time, event).
    anchor: tuple[Station, float, str] | None = None
    platform: str | None = None
    updated: float = field(default_factory=time.time)
    # Matched timetable entry and the date it runs (from its origin).
    service: tuple[Schedule, date] | None = None


class TrainTracker:
    def __init__(
        self,
        *,
        corpus: dict[str, Location],
        stations: dict[str, Station],
        home: tuple[float, float],
        radius_km: float,
        watch: tuple[str, str] | None,
        alert_seconds: float,
        rail: RailNetwork | None = None,
        info: ServiceInfo | None = None,
        boards: list[str] = (),
    ):
        self.corpus = corpus
        self.rail = rail
        self.info = info
        self.timetable: Timetable | None = None
        # TRUST train ID → (schedule UID, run date, when activated).
        self.activations: dict[str, tuple[str, date, float]] = {}
        # (schedule UID, run date) of trains TRUST has reported cancelled.
        self.cancelled: set[tuple[str, date]] = set()
        self.stations = stations
        self.home = home
        self.radius_km = radius_km
        self.alert_seconds = alert_seconds
        self.trains: dict[str, TrainState] = {}
        self.status: dict = {"ok": None, "connected": False, "messages": 0, "last_message": None, "error": None}
        self._lock = threading.Lock()
        self._alerted: dict[str, float] = {}

        self.watch: tuple[Station, Station] | None = None
        self.home_line: HomeLine | None = None
        self.fraction = 0.0
        self.offset_km: float | None = None
        if watch:
            missing = [c for c in watch if c not in stations]
            if missing:
                raise ValueError(f"Unknown station code(s) in TRAIN_WATCH: {', '.join(missing)}")
            self.watch = (stations[watch[0]], stations[watch[1]])
            self._place_home()

        # Stations within the radius, for drawing on the map.
        self.area_stations = stations_within(stations, *home, radius_km)
        unknown = [c for c in boards if c not in stations]
        if unknown:
            raise ValueError(f"Unknown station code(s) in TRAIN_BOARDS: {', '.join(unknown)}")
        self.board_stations = [stations[c] for c in boards]

    def _place_home(self) -> None:
        """Where home sits along the watched stretch: by track distance when we have the track."""
        a, b = self.watch
        track = self.track(a, b)
        if track:
            self.fraction, offset = fraction_along(track, *self.home)
            point = point_along(track, self.fraction)
        else:
            self.fraction = project_fraction(a, b, *self.home)
            point = (a.lat + self.fraction * (b.lat - a.lat), a.lon + self.fraction * (b.lon - a.lon))
            offset = haversine_km(*self.home, *point)
        self.offset_km = round(offset, 2)
        # The spot on the line trains pass; any route through it counts, not just watch A↔B.
        self.home_line = HomeLine(point, self.track, self.info, self.stations)

    @property
    def watch_warning(self) -> str | None:
        """Why "due past home" may not match where the house really is, if it looks off."""
        if not self.watch:
            return None
        a, b = self.watch
        if self.fraction <= 0.01 or self.fraction >= 0.99:
            nearer = a if self.fraction <= 0.01 else b
            # Right next to that station is fine: its trains pass (or stop) by the house.
            if haversine_km(*self.home, nearer.lat, nearer.lon) > 0.3:
                return (f"Home isn't between {a.name} and {b.name}: it's beyond {nearer.name}. "
                        "Choose the two stations either side of you.")
        if self.offset_km is not None and self.offset_km > 2:
            return (f"Home is {self.offset_km:.1f} km from the {a.name}–{b.name} line, "
                    "so trains on it may not pass close by.")
        return None

    def set_rail(self, rail: RailNetwork) -> None:
        """Track data arrived (possibly after startup): route trains and home along it from now on."""
        with self._lock:
            self.rail = rail
            if self.watch:
                self._place_home()

    def set_timetable(self, timetable: Timetable) -> None:
        with self._lock:
            self.timetable = timetable
            # Re-match everything: schedules may have changed in the update.
            for s in self.trains.values():
                s.service = None
                self._match(s)

    def track(self, a: Station, b: Station) -> list[tuple[float, float]] | None:
        return self.rail.path(a, b) if self.rail else None

    def station_for(self, stanox: str | None) -> Station | None:
        loc = self.corpus.get(stanox or "")
        return self.stations.get(loc.crs) if loc and loc.crs else None

    def location_name(self, stanox: str | None) -> str | None:
        station = self.station_for(stanox)
        if station:
            return station.name
        loc = self.corpus.get(stanox or "")
        return loc.name if loc else None

    # ---------- ingest (STOMP thread) ----------

    def ingest(self, messages: list[dict], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self.status["messages"] += len(messages)
            self.status["last_message"] = now
            for msg in messages:
                msg_type = (msg.get("header") or {}).get("msg_type")
                body = msg.get("body") or {}
                if msg_type == MOVEMENT:
                    m = parse_movement(body)
                    if m and abs(now - m.time) <= MAX_REPORT_AGE:
                        self._apply(m, now)
                elif msg_type == ACTIVATION:
                    activation = parse_activation(body)
                    if activation:
                        train_id, uid, run_date = activation
                        self.activations[train_id] = (uid, run_date, now)
                elif msg_type == CANCELLATION:
                    state = self.trains.pop(body.get("train_id"), None)
                    if state and state.service:
                        self.cancelled.add((state.service[0].uid, state.service[1]))
                    elif body.get("train_id") in self.activations:
                        uid, run_date, _ = self.activations[body["train_id"]]
                        self.cancelled.add((uid, run_date))

    def _apply(self, m: Movement, now: float) -> None:
        state = self.trains.get(m.train_id)
        if state is None:
            state = self.trains[m.train_id] = TrainState(m.train_id, m)
        elif m.time < state.last.time:
            return  # out-of-order or a late correction
        state.last = m
        state.updated = now
        # A platform belongs to the station it was reported at; don't carry it to the next one.
        state.platform = m.platform
        station = self.station_for(m.stanox)
        if station:
            state.anchor = (station, m.time, m.event)
        if state.service is None:
            self._match(state)

    def _match(self, state: TrainState) -> None:
        if not self.timetable or not self.info:
            return
        activation = self.activations.get(state.train_id)
        if activation:
            schedule, cancelled = self.timetable.schedule_for(activation[0], activation[1])
            if schedule and not cancelled:
                state.service = (schedule, activation[1])
                return
        m = state.last
        state.service = self.timetable.match(
            headcode(state.train_id), self.info.by_stanox.get(m.stanox, set()), m.time, m.late_minutes)

    def _left_index(self, state: TrainState) -> int | None:
        """Index of the last timetable call the train has left (or passed)."""
        if not state.service:
            return None
        schedule, run_date = state.service
        m = state.last
        i = self.info.call_index(schedule, m.stanox, m.time, run_date)
        if i is None:
            return None
        return i - 1 if m.event == "ARRIVAL" else i

    # ---------- positions ----------

    def _locate(self, s: TrainState, now: float) -> dict | None:
        m = s.last
        if m.terminated:
            if now - m.time > TERMINATED_SECONDS or not s.anchor:
                return None
            return self._at(s.anchor[0], "terminated")
        if now - m.time > STALE_SECONDS:
            return None

        here = self.station_for(m.stanox)
        nxt = self.station_for(m.next_stanox)
        eta = m.next_eta

        if here and m.event == "ARRIVAL":
            return self._at(here, "at")

        # Travelling: from the last station seen towards the next station due.
        start = s.anchor
        if start and nxt and eta and start[0].crs != nxt.crs:
            t0, t1 = start[1], eta
            if now >= t1:
                if now - t1 > OVERDUE_SECONDS:
                    return None
                return self._at(nxt, "approaching") | {"next_time": t1}
            f = (now - t0) / (t1 - t0) if t1 > t0 else 1.0
            f = min(max(f, 0.0), 1.0)
            a, b = start[0], nxt
            track = self.track(a, b)
            lat, lon = point_along(track, f) if track else (a.lat + f * (b.lat - a.lat), a.lon + f * (b.lon - a.lon))
            return {
                "state": "between",
                "lat": lat,
                "lon": lon,
                "on_track": track is not None,
                "from": a.crs,
                "to": b.crs,
                "progress": f,
                "next_time": t1,
                "segment_start": t0,
            }

        # Departed but the next report point has no coordinates: show briefly at the station.
        if here and now - m.time <= 60:
            return self._at(here, "departing")
        return None

    @staticmethod
    def _at(station: Station, state: str) -> dict:
        return {"state": state, "lat": station.lat, "lon": station.lon, "at": station.crs}

    def _describe(self, s: TrainState, where: dict) -> dict:
        m = s.last
        code = headcode(s.train_id)
        timetable = {}
        if s.service:
            timetable = self.info.summary(s.service[0], s.service[1], m.late_minutes, self._left_index(s))
        return {
            "uid": s.train_id,
            "headcode": code,
            "kind": train_class(code),
            "freight": code[:1] in "045678",
            "late_minutes": m.late_minutes,
            "last_report": self.location_name(m.stanox),
            "last_report_time": m.time,
            "next": self.location_name(m.next_stanox),
            "platform": s.platform,
            **timetable,
            **where,
        }

    def _home_pass(self, where: dict) -> float | None:
        """Estimated time passing home, if the train is on a stretch of track that runs past it."""
        if not self.home_line or where.get("state") != "between":
            return None
        f = self.home_line.fraction(self.stations[where["from"]], self.stations[where["to"]])
        if f is None:
            return None
        t0, t1 = where["segment_start"], where["next_time"]
        return t0 + f * (t1 - t0)

    def _scheduled_passes(self, now: float, live: dict) -> list[dict]:
        """Trains due past home in the next hour from the timetable, with live delays where known.

        `live` maps (schedule UID, run date) to the tracked TrainState.
        """
        if not self.home_line or not self.timetable or not self.info:
            return []
        today = datetime.fromtimestamp(now, UK).date()
        passes = []
        for run_date in (today - timedelta(days=1), today):
            for ts, s, a, b, planned_cancel in self.home_line.scheduled(self.timetable, run_date):
                if not now - 3600 <= ts <= now + PASS_HORIZON:
                    continue
                state = live.get((s.uid, run_date))
                late = state.last.late_minutes if state else None
                expected = ts + (late or 0) * 60
                if not now - 60 <= expected <= now + PASS_HORIZON:
                    continue
                code = s.headcode or "????"
                passes.append({
                    "uid": state.train_id if state else f"{s.uid}:{run_date}",
                    "headcode": code,
                    "kind": train_class(code),
                    "freight": s.atoc == "ZZ" or code[:1] in "045678",
                    "destination": self.info.name(s.calls[-1].tiploc) if s.calls else None,
                    "operator": s.details()["operator"],
                    "at_home": expected,
                    "scheduled_at_home": ts,
                    "late_minutes": late,
                    "live": state is not None,
                    "cancelled": planned_cancel or (s.uid, run_date) in self.cancelled,
                    "from": a.crs,
                    "to": b.crs,
                })
        return passes

    def _live_map(self) -> dict:
        """(schedule UID, run date) → (lateness, index of the last call left) for tracked trains."""
        return {(s.service[0].uid, s.service[1]): (s.last.late_minutes, self._left_index(s))
                for s in self.trains.values() if s.service}

    def _boards(self, now: float) -> list[dict]:
        if not self.timetable or not self.info or not self.board_stations:
            return []
        live = self._live_map()
        return [self.info.board(self.timetable, station, now, live, self.cancelled)
                for station in self.board_stations]

    def _platform_at(self, s: TrainState, crs: str) -> tuple[str | None, bool]:
        """(platform at this station, whether it's the one actually reported rather than planned)."""
        here = self.station_for(s.last.stanox)
        if here and here.crs == crs and s.last.platform:
            return s.last.platform, True
        if s.service and self.info:
            tiplocs = self.info.by_crs.get(crs, set())
            for c in s.service[0].calls:
                if c.tiploc in tiplocs and c.platform:
                    return c.platform, False
        return None, False

    def station_view(self, crs: str, now: float | None = None) -> dict:
        """Trains at (or about to reach) one station, with their platforms, and its departures."""
        now = time.time() if now is None else now
        with self._lock:
            trains = []
            for s in self.trains.values():
                where = self._locate(s, now)
                if where is None:
                    continue
                at_station = where.get("at") == crs
                arriving = (where.get("state") == "between" and where.get("to") == crs
                            and where["next_time"] - now <= ARRIVING_SECONDS)
                if not (at_station or arriving):
                    continue
                platform, confirmed = self._platform_at(s, crs)
                train = self._describe(s, where)
                train.update(platform=platform, platform_confirmed=confirmed,
                             status=where["state"] if at_station else "arriving")
                trains.append(train)
            station = self.stations.get(crs)
            board = None
            if station and self.timetable and self.info:
                board = self.info.board(self.timetable, station, now, self._live_map(), self.cancelled)
        order = {"at": 0, "terminated": 1, "departing": 2, "approaching": 3, "arriving": 4}
        trains.sort(key=lambda t: (order.get(t["status"], 9), t["headcode"]))
        return {"trains": trains, "board": board}

    def snapshot(self, now: float | None = None) -> tuple[dict, list[dict]]:
        """(state for the browser, newly due "past home" alerts)."""
        now = time.time() if now is None else now
        with self._lock:
            positions, passes = [], []
            for uid, s in list(self.trains.items()):
                if now - s.updated > STALE_SECONDS:
                    del self.trains[uid]
                    continue
                where = self._locate(s, now)
                if where is None:
                    continue
                if haversine_km(*self.home, where["lat"], where["lon"]) > self.radius_km:
                    continue
                train = self._describe(s, where)
                positions.append(train)
                at_home = self._home_pass(where)
                if at_home is not None and at_home >= now - 60:
                    passes.append(train | {"at_home": at_home, "live": True, "cancelled": False})
            # Timetable passes for the next hour. A tracked train has the same uid (its TRUST ID)
            # in both lists, so one already counted from its live position is skipped here.
            live = {(st.service[0].uid, st.service[1]): st for st in self.trains.values() if st.service}
            seen = {p["uid"] for p in passes}
            passes += [p for p in self._scheduled_passes(now, live) if p["uid"] not in seen]
            status = dict(self.status, tracked=len(self.trains),
                          matched=sum(1 for s in self.trains.values() if s.service),
                          timetable=self.timetable is not None)
            boards = self._boards(now)
            self.activations = {k: v for k, v in self.activations.items() if now - v[2] < ACTIVATION_MAX_AGE}
            yesterday = datetime.fromtimestamp(now - 86400, UK).date()
            self.cancelled = {c for c in self.cancelled if c[1] >= yesterday}

        positions.sort(key=lambda t: (t["state"] != "between", t["headcode"]))
        passes.sort(key=lambda p: p["at_home"])
        # Only alert for trains actually seen running, not timetable entries that might not run.
        alerts = [p for p in passes if p["live"] and not p["cancelled"]
                  and 0 <= p["at_home"] - now <= self.alert_seconds and p["uid"] not in self._alerted]
        for p in alerts:
            self._alerted[p["uid"]] = now
        self._alerted = {uid: t for uid, t in self._alerted.items() if now - t < 3600}

        state: dict = {
            "status": status,
            "radius_km": self.radius_km,
            "stations": [s.as_dict() for s in self.area_stations],
            "positions": positions,
            "boards": boards,
            "watch": None,
        }
        if self.watch:
            a, b = self.watch
            state["watch"] = {"a": a.as_dict(), "b": b.as_dict(), "fraction": round(self.fraction, 3),
                              "offset_km": self.offset_km, "passes": passes, "warning": self.watch_warning,
                              "track": self.track(a, b) or [[a.lat, a.lon], [b.lat, b.lon]]}
        return state, alerts
