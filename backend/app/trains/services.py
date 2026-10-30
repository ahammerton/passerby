"""Timetable details for live trains, and departure boards, ready for the browser."""

from datetime import date, datetime, timedelta

from .corpus import Tiploc
from .stations import Station
from .timetable import UK, Schedule, Timetable, run_seconds_to_ts

# Departure boards show this far ahead.
BOARD_MINUTES = 120
BOARD_ROWS = 12


class ServiceInfo:
    def __init__(self, tiplocs: dict[str, Tiploc], stations: dict[str, Station]):
        self.tiplocs = tiplocs
        self.stations = stations
        self.by_stanox: dict[str, set[str]] = {}
        self.by_crs: dict[str, set[str]] = {}
        # Platform-group TIPLOCs (WATRLMN, "Waterloo Main") have no CRS of their own; their
        # station's CRS is on a sibling TIPLOC with the same STANOX.
        stanox_crs: dict[str, str] = {}
        for t in tiplocs.values():
            if t.stanox:
                self.by_stanox.setdefault(t.stanox, set()).add(t.tiploc)
                if t.crs and t.crs in stations:
                    stanox_crs.setdefault(t.stanox, t.crs)
        self.crs_of: dict[str, str] = {}
        for t in tiplocs.values():
            crs = t.crs if t.crs in stations else stanox_crs.get(t.stanox or "")
            if crs:
                self.crs_of[t.tiploc] = crs
                self.by_crs.setdefault(crs, set()).add(t.tiploc)
        self._boards: dict[tuple[str, date, int], list] = {}

    def name(self, tiploc: str) -> str:
        """A station's proper name ("London Waterloo"), else Network Rail's ("Woking Junction")."""
        crs = self.crs_of.get(tiploc)
        if crs:
            return self.stations[crs].name
        t = self.tiplocs.get(tiploc)
        return t.name if t else tiploc

    def call_index(self, s: Schedule, stanox: str | None, near: float, run_date: date) -> int | None:
        """Which of the schedule's calls a TRUST report at `stanox` refers to (nearest in time)."""
        tiplocs = self.by_stanox.get(stanox or "", set())
        best, best_gap = None, float("inf")
        for i, c in enumerate(s.calls):
            if c.tiploc in tiplocs and c.time is not None:
                gap = abs(run_seconds_to_ts(run_date, c.time) - near)
                if gap < best_gap:
                    best, best_gap = i, gap
        return best

    def summary(self, s: Schedule, run_date: date, late_minutes: int | None, left_index: int | None) -> dict:
        """Origin, destination, operator details and calling points with expected times."""
        calls = []
        for i, c in enumerate(s.calls):
            if not c.public:
                continue
            sched = c.public_departure if c.public_departure is not None else c.public_arrival
            ts = run_seconds_to_ts(run_date, sched)
            passed = left_index is not None and i <= left_index
            expected = None
            if not passed and late_minutes and late_minutes > 0:
                # Assume the current delay carries on to the stops still to come.
                expected = ts + late_minutes * 60
            calls.append({"name": self.name(c.tiploc), "time": ts, "expected": expected,
                          "passed": passed, "platform": c.platform})
        return {
            "origin": self.name(s.calls[0].tiploc) if s.calls else None,
            "destination": self.name(s.calls[-1].tiploc) if s.calls else None,
            "bus": s.bus,
            **s.details(),
            "calls": calls,
        }

    # ---------- departure boards ----------

    def _board_base(self, tt: Timetable, crs: str, run_date: date) -> list:
        """(time, schedule, call, run date, planned-cancelled) for every departure on a date."""
        key = (crs, run_date, tt.sequence)
        if key not in self._boards:
            tiplocs = self.by_crs.get(crs, set())
            rows = []
            for s, cancelled in tt.running_on(run_date):
                for i, c in enumerate(s.calls):
                    # Departures only: the last call is where the train terminates.
                    if c.tiploc in tiplocs and c.public_departure is not None and i < len(s.calls) - 1:
                        rows.append((run_seconds_to_ts(run_date, c.public_departure), s, i, run_date, cancelled))
            rows.sort(key=lambda r: r[0])
            # Drop boards for other dates or an older timetable.
            self._boards = {k: v for k, v in self._boards.items() if k[1] >= run_date - timedelta(days=1)}
            self._boards[key] = rows
        return self._boards[key]

    def board(self, tt: Timetable, station: Station, now: float, live: dict, cancelled: set) -> dict:
        """Next departures from a station, with live running where the train is being tracked.

        `live` maps (uid, run date) to (lateness in minutes, index of the last call it has left).
        `cancelled` holds (uid, run date) for trains TRUST has reported cancelled.
        """
        today = datetime.fromtimestamp(now, UK).date()
        rows = []
        for run_date in (today - timedelta(days=1), today):  # yesterday's trains still running after midnight
            for ts, s, i, rd, planned_cancel in self._board_base(tt, station.crs, run_date):
                if not now - 120 <= ts <= now + BOARD_MINUTES * 60:
                    continue
                late, left = live.get((s.uid, rd), (None, None))
                if left is not None and left >= i:
                    continue  # already left this station
                is_cancelled = planned_cancel or (s.uid, rd) in cancelled
                rows.append({
                    "time": ts,
                    "expected": ts + late * 60 if late and late > 0 else None,
                    "late_minutes": late,
                    "live": late is not None,
                    "cancelled": is_cancelled,
                    "headcode": s.headcode,
                    "destination": self.name(s.calls[-1].tiploc),
                    "platform": s.calls[i].platform,
                    "operator": s.details()["operator"],
                    "bus": s.bus,
                })
        rows.sort(key=lambda r: r["time"])
        return {**station.as_dict(), "departures": rows[:BOARD_ROWS]}
