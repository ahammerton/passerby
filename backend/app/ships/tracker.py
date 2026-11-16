"""The ships near home: positions joined with their details, short trails, and expiry.

aisstream.io passes on a ship's position only every 2-3 minutes (sometimes 6-9), so ships
stay listed for a while after their last report, and the browser moves them on by their
speed and course in between. Ship details (name, type, size) arrive every few minutes at
best, so they're also kept on disk (ships.db): after a restart ships are known at once.
"""

import sqlite3
import threading
import time
from pathlib import Path

from ..geo import bearing_deg, compass, haversine_km
from . import ais

# Seconds without a position report before a ship is dropped.
EXPIRE_MOVING = 30 * 60
EXPIRE_STILL = 60 * 60  # moored and anchored ships report less often
TRAIL_POINTS = 20
TRAIL_SECONDS = 30 * 60
TRAIL_MIN_KM = 0.05  # a new trail point once a ship has moved this far
STILL_KT = 0.5

DETAIL_KEYS = ("name", "callsign", "imo", "type_code", "length_m", "beam_m", "draught_m", "destination", "eta")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ships (
    mmsi INTEGER PRIMARY KEY,
    name TEXT, callsign TEXT, imo INTEGER, type_code INTEGER,
    length_m REAL, beam_m REAL, draught_m REAL, destination TEXT, eta REAL,
    updated REAL NOT NULL
);
"""


class ShipDetails:
    """Ship details kept on disk. Written from a worker thread, so every use holds the lock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def load(self) -> dict[int, dict]:
        with self._lock:
            rows = self.db.execute(f"SELECT mmsi, {', '.join(DETAIL_KEYS)} FROM ships").fetchall()
        return {r[0]: {k: v for k, v in zip(DETAIL_KEYS, r[1:]) if v is not None} for r in rows}

    def save(self, details: dict[int, dict], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self.db:
            self.db.executemany(
                f"INSERT OR REPLACE INTO ships (mmsi, {', '.join(DETAIL_KEYS)}, updated)"
                f" VALUES (?, {', '.join('?' * len(DETAIL_KEYS))}, ?)",
                [(mmsi, *(d.get(k) for k in DETAIL_KEYS), now) for mmsi, d in details.items()])

    def close(self) -> None:
        with self._lock:
            self.db.close()


class ShipTracker:
    def __init__(self, home_lat: float, home_lon: float, radius_km: float, show_leisure: bool = False,
                 details: dict[int, dict] | None = None):
        self.home = (home_lat, home_lon)
        self.radius_km = radius_km
        self.show_leisure = show_leisure
        self.positions: dict[int, dict] = {}  # mmsi -> latest position, within range
        self.trails: dict[int, list[tuple[float, float, float]]] = {}
        self.details: dict[int, dict] = details or {}
        self.dirty: set[int] = set()  # details not yet saved to disk
        self.status: dict = {"ok": None, "connected": False, "messages": 0, "last_message": None, "error": None}

    def ingest(self, msg: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.status["messages"] += 1
        self.status["last_message"] = now
        parsed = ais.parse(msg, now)
        if not parsed:
            return
        mmsi, position, details = parsed
        if details:
            known = self.details.setdefault(mmsi, {})
            if any(known.get(k) != v for k, v in details.items()):
                known.update(details)
                self.dirty.add(mmsi)
        if position:
            self._move(mmsi, position)

    def _move(self, mmsi: int, p: dict) -> None:
        distance = haversine_km(*self.home, p["lat"], p["lon"])
        if distance > self.radius_km:  # the subscription is a box; the corners are out of range
            self.positions.pop(mmsi, None)
            self.trails.pop(mmsi, None)
            return
        previous = self.positions.get(mmsi)
        if previous and p["reported"] < previous["reported"]:
            return  # an older report, relayed late
        self.positions[mmsi] = p | {"distance_km": round(distance, 1)}
        trail = self.trails.setdefault(mmsi, [])
        if not trail or haversine_km(trail[-1][0], trail[-1][1], p["lat"], p["lon"]) >= TRAIL_MIN_KM:
            trail.append((p["lat"], p["lon"], p["reported"]))
        del trail[:-TRAIL_POINTS]

    def expire(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for mmsi, p in list(self.positions.items()):
            still = p["still"] or (p["speed_kt"] is not None and p["speed_kt"] < STILL_KT)
            if now - p["reported"] > (EXPIRE_STILL if still else EXPIRE_MOVING):
                del self.positions[mmsi]
                self.trails.pop(mmsi, None)

    def take_dirty(self) -> dict[int, dict]:
        """Details changed since the last call, to save."""
        changed = {m: dict(self.details[m]) for m in self.dirty}
        self.dirty.clear()
        return changed

    def ship(self, mmsi: int, now: float | None = None) -> dict | None:
        p = self.positions.get(mmsi)
        if p is None:
            return None
        d = self.details.get(mmsi, {})
        category, type_name = ais.ship_type(d.get("type_code"))
        if category == "unknown" and p["ais_class"] == "B":
            category = "small"  # small boats that don't say what they are: mostly leisure
        if beacon := ais.beacon(mmsi):
            category, type_name = "beacon", beacon
        bearing = bearing_deg(*self.home, p["lat"], p["lon"])
        return {
            "mmsi": mmsi, "name": d.get("name"), "callsign": d.get("callsign"), "imo": d.get("imo"),
            "flag": ais.flag(mmsi), "category": category, "type": type_name, "type_code": d.get("type_code"),
            "length_m": d.get("length_m"), "beam_m": d.get("beam_m"), "draught_m": d.get("draught_m"),
            "destination": d.get("destination"), "eta": d.get("eta"),
            "lat": p["lat"], "lon": p["lon"], "speed_kt": p["speed_kt"], "course": p["course"],
            "heading": p["heading"], "status": p["status"], "still": p["still"], "ais_class": p["ais_class"],
            "reported": p["reported"], "distance_km": p["distance_km"], "direction": compass(bearing),
        }

    def visible(self, ship: dict) -> bool:
        return self.show_leisure or ship["category"] not in ais.LEISURE | {"small"}

    def snapshot(self, now: float | None = None) -> dict:
        self.expire(now)
        ships = [s for s in (self.ship(m) for m in self.positions) if s and self.visible(s)]
        ships.sort(key=lambda s: s["distance_km"])
        status = dict(self.status, tracked=len(self.positions), shown=len(ships))
        return {"ships": ships, "status": status}

    def trail(self, mmsi: int) -> list[list[float]]:
        now = time.time()
        return [[lat, lon] for lat, lon, t in self.trails.get(mmsi, []) if now - t <= TRAIL_SECONDS]
