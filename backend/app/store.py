"""SQLite log of every aircraft seen, one row per visit."""

import sqlite3
import threading
import time
from pathlib import Path

from .models import Aircraft

# A gap longer than this starts a new visit for the same aircraft.
VISIT_GAP_SECONDS = 30 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY,
    icao24 TEXT NOT NULL,
    callsign TEXT,
    registration TEXT,
    type_code TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    closest_km REAL NOT NULL,
    max_altitude_ft REAL,
    was_overhead INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sightings_icao_last ON sightings (icao24, last_seen);
CREATE INDEX IF NOT EXISTS sightings_last ON sightings (last_seen);
"""


class SightingStore:
    """Used from worker threads (record) and request threads (recent, stats) alike, so every use of
    the one connection holds the lock: SQLite crashes the process if two threads share a connection
    at once, or if it's closed mid-query."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._open_visits: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, snapshot: list[Aircraft], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self.db:
            for a in snapshot:
                visit_id = self._open_visits.get(a.icao24)
                if visit_id is not None:
                    row = self.db.execute(
                        "SELECT last_seen FROM sightings WHERE id = ?", (visit_id,)
                    ).fetchone()
                    if row is None or now - row["last_seen"] > VISIT_GAP_SECONDS:
                        visit_id = None
                if visit_id is None:
                    cur = self.db.execute(
                        "INSERT INTO sightings (icao24, callsign, registration, type_code,"
                        " first_seen, last_seen, closest_km, max_altitude_ft, was_overhead)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (a.icao24, a.callsign, a.registration, a.type_code, now, now,
                         a.distance_km, a.altitude_ft, int(a.overhead)),
                    )
                    self._open_visits[a.icao24] = cur.lastrowid
                else:
                    self.db.execute(
                        "UPDATE sightings SET"
                        " last_seen = ?,"
                        " callsign = COALESCE(?, callsign),"
                        " registration = COALESCE(?, registration),"
                        " type_code = COALESCE(?, type_code),"
                        " closest_km = MIN(closest_km, ?),"
                        " max_altitude_ft = MAX(COALESCE(max_altitude_ft, 0), COALESCE(?, 0)),"
                        " was_overhead = MAX(was_overhead, ?)"
                        " WHERE id = ?",
                        (now, a.callsign, a.registration, a.type_code, a.distance_km,
                         a.altitude_ft, int(a.overhead), visit_id),
                    )

    def recent(self, limit: int = 100, overhead_only: bool = False) -> list[dict]:
        where = "WHERE was_overhead = 1" if overhead_only else ""
        with self._lock:
            rows = self.db.execute(
                f"SELECT * FROM sightings {where} ORDER BY last_seen DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, since: float) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS visits, COUNT(DISTINCT icao24) AS aircraft,"
                " SUM(was_overhead) AS overhead FROM sightings WHERE last_seen >= ?",
                (since,),
            ).fetchone()
        return {k: row[k] or 0 for k in row.keys()}

    def type_history(self, since: float) -> tuple[dict[str, int], int, float]:
        """(type code -> different airframes seen since `since`, aircraft ever seen, days of history)."""
        with self._lock:
            counts = dict(self.db.execute(
                "SELECT UPPER(type_code), COUNT(DISTINCT icao24) FROM sightings"
                " WHERE type_code IS NOT NULL AND last_seen >= ? GROUP BY UPPER(type_code)", (since,)).fetchall())
            aircraft, first, last = self.db.execute(
                "SELECT COUNT(DISTINCT icao24), MIN(first_seen), MAX(last_seen) FROM sightings").fetchone()
        return counts, aircraft, ((last - first) / 86400 if first is not None else 0.0)

    def close(self) -> None:
        # Waits for a record() still running in a worker thread after its task was cancelled.
        with self._lock:
            self.db.close()
