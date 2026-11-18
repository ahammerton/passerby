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
CREATE TABLE IF NOT EXISTS train_passes (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    headcode TEXT,
    operator TEXT,
    origin TEXT,
    destination TEXT,
    late_minutes INTEGER,
    freight INTEGER NOT NULL DEFAULT 0,
    passed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS train_passes_at ON train_passes (passed_at);
"""

# Columns added after the first release; older databases gain them on start-up.
LATER_COLUMNS = {
    "aircraft_name": "TEXT", "airline_name": "TEXT", "origin": "TEXT", "destination": "TEXT",
    "interest_level": "TEXT", "interest_reason": "TEXT",
}

# Busiest-hour and per-day counts use the server's local time (TZ).
LOCAL_HOUR = "CAST(strftime('%H', first_seen, 'unixepoch', 'localtime') AS INTEGER)"
LOCAL_DAY = "date(first_seen, 'unixepoch', 'localtime')"


def _place(end) -> str | None:
    """An airport as shown in history: its IATA code (or ICAO), then the city."""
    if end is None:
        return None
    code = end.iata or end.icao
    where = end.city or end.name
    return " ".join(x for x in (code, where and f"({where})") if x) or None


class SightingStore:
    """Used from worker threads (record) and request threads (recent, stats) alike, so every use of
    the one connection holds the lock: SQLite crashes the process if two threads share a connection
    at once, or if it's closed mid-query."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(sightings)")}
        for name, kind in LATER_COLUMNS.items():
            if name not in have:
                self.db.execute(f"ALTER TABLE sightings ADD COLUMN {name} {kind}")
        self.db.commit()
        self._open_visits: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, snapshot: list[Aircraft], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self.db:
            for a in snapshot:
                level = (a.interest or {}).get("level")
                extra = (a.aircraft_name, a.airline_name,
                         _place(a.route.origin) if a.route else None,
                         _place(a.route.destination) if a.route else None,
                         level, (a.interest or {}).get("reason"))
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
                        " first_seen, last_seen, closest_km, max_altitude_ft, was_overhead,"
                        " aircraft_name, airline_name, origin, destination, interest_level, interest_reason)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (a.icao24, a.callsign, a.registration, a.type_code, now, now,
                         a.distance_km, a.altitude_ft, int(a.overhead), *extra),
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
                        " was_overhead = MAX(was_overhead, ?),"
                        " aircraft_name = COALESCE(?, aircraft_name),"
                        " airline_name = COALESCE(?, airline_name),"
                        " origin = COALESCE(?, origin),"
                        " destination = COALESCE(?, destination),"
                        # The most serious reason seen during the visit wins (emergency over the rest).
                        " interest_reason = CASE WHEN ? IS NOT NULL AND (interest_level IS NULL OR ? = 'emergency')"
                        "   THEN ? ELSE interest_reason END,"
                        " interest_level = CASE WHEN ? IS NOT NULL AND (interest_level IS NULL OR ? = 'emergency')"
                        "   THEN ? ELSE interest_level END"
                        " WHERE id = ?",
                        (now, a.callsign, a.registration, a.type_code, a.distance_km,
                         a.altitude_ft, int(a.overhead), *extra[:4],
                         level, level, extra[5], level, level, level, visit_id),
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

    def record_train_passes(self, passes: list[dict], now: float | None = None) -> None:
        """Trains seen passing home (the ones that raise a "train passing" alert)."""
        if not passes:
            return
        now = time.time() if now is None else now
        with self._lock, self.db:
            self.db.executemany(
                "INSERT INTO train_passes (uid, headcode, operator, origin, destination, late_minutes, freight,"
                " passed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(p.get("uid"), p.get("headcode"), p.get("operator"), p.get("origin"), p.get("destination"),
                  p.get("late_minutes"), int(bool(p.get("freight"))), p.get("at_home") or now) for p in passes])

    def summary(self, since: float, until: float, list_limit: int = 300) -> dict:
        """Everything the history page shows for one period."""
        def q(sql: str, args: tuple) -> list:
            return self.db.execute(sql, args).fetchall()

        with self._lock:
            # A visit belongs to the period if any part of it falls inside.
            where = "WHERE first_seen < ? AND last_seen >= ?"
            span = (until, since)
            totals = q("SELECT COUNT(*), COUNT(DISTINCT icao24), COALESCE(SUM(was_overhead), 0),"
                       " MIN(first_seen) FROM sightings " + where, span)[0]
            by_hour = q(f"SELECT {LOCAL_HOUR} AS h, COUNT(*), SUM(was_overhead) FROM sightings {where} GROUP BY h", span)
            by_day = q(f"SELECT {LOCAL_DAY} AS d, COUNT(DISTINCT icao24), SUM(was_overhead) FROM sightings {where}"
                       " GROUP BY d ORDER BY d", span)
            airlines = q("SELECT UPPER(SUBSTR(callsign, 1, 3)) AS code, MAX(airline_name),"
                         " COUNT(DISTINCT icao24) AS n FROM sightings " + where +
                         " AND callsign GLOB '[A-Z][A-Z][A-Z][0-9]*' GROUP BY code ORDER BY n DESC LIMIT 10", span)
            types = q("SELECT UPPER(type_code) AS code, MAX(aircraft_name), COUNT(DISTINCT icao24) AS n FROM sightings "
                      + where + " AND type_code IS NOT NULL AND type_code != '' GROUP BY code ORDER BY n DESC LIMIT 10",
                      span)
            routes = q("SELECT origin, destination, COUNT(*) AS n FROM sightings " + where +
                       " AND origin IS NOT NULL AND destination IS NOT NULL GROUP BY origin, destination"
                       " ORDER BY n DESC LIMIT 10", span)
            interesting = q("SELECT * FROM sightings " + where + " AND interest_level IS NOT NULL"
                            " ORDER BY last_seen DESC LIMIT ?", (*span, list_limit))
            overhead = q("SELECT * FROM sightings " + where + " AND was_overhead = 1"
                         " ORDER BY last_seen DESC LIMIT ?", (*span, list_limit))
            first_ever = self.db.execute("SELECT MIN(first_seen) FROM sightings").fetchone()[0]

            t_where = "WHERE passed_at >= ? AND passed_at < ?"
            t_span = (since, until)
            t_total = q("SELECT COUNT(*), SUM(freight), AVG(CASE WHEN late_minutes > 0 THEN 1.0 ELSE 0 END)"
                        " FROM train_passes " + t_where, t_span)[0]
            t_hour = q("SELECT CAST(strftime('%H', passed_at, 'unixepoch', 'localtime') AS INTEGER) AS h, COUNT(*)"
                       " FROM train_passes " + t_where + " GROUP BY h", t_span)
            t_ops = q("SELECT COALESCE(operator, CASE WHEN freight THEN 'Freight' ELSE 'Other' END) AS o, COUNT(*) AS n"
                      " FROM train_passes " + t_where + " GROUP BY o ORDER BY n DESC LIMIT 10", t_span)
            t_recent = q("SELECT * FROM train_passes " + t_where + " ORDER BY passed_at DESC LIMIT ?",
                         (*t_span, list_limit))
        hours = [[0, 0] for _ in range(24)]
        for h, n, o in by_hour:
            hours[h] = [n, o or 0]
        t_hours = [0] * 24
        for h, n in t_hour:
            t_hours[h] = n
        return {
            "first_record": first_ever,
            "aircraft": {
                "sightings": totals[0], "aircraft": totals[1], "overhead": totals[2],
                "by_hour": [{"hour": h, "sightings": n, "overhead": o} for h, (n, o) in enumerate(hours)],
                "by_day": [{"date": d, "aircraft": n, "overhead": o or 0} for d, n, o in by_day],
                "airlines": [{"code": c, "name": name, "aircraft": n} for c, name, n in airlines],
                "types": [{"code": c, "name": name, "aircraft": n} for c, name, n in types],
                "routes": [{"origin": a, "destination": b, "flights": n} for a, b, n in routes],
                "interesting": [dict(r) for r in interesting],
                "overhead_list": [dict(r) for r in overhead],
            },
            "trains": {
                "passes": t_total[0], "freight": t_total[1] or 0,
                "late_share": t_total[2],
                "by_hour": [{"hour": h, "passes": n} for h, n in enumerate(t_hours)],
                "operators": [{"name": o, "passes": n} for o, n in t_ops],
                "recent": [dict(r) for r in t_recent],
            },
        }

    def close(self) -> None:
        # Waits for a record() still running in a worker thread after its task was cancelled.
        with self._lock:
            self.db.close()
