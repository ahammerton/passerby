"""Trains passing home: which stretches of track run past it, and when trains are due.

"Home" is the point on the watched line's track nearest the house. Any train
whose route between two consecutive stations runs through that point passes
home, whatever those stations are (a non-stop train timed only at the stations
either side still passes a house beside an intermediate station).

Passes come from two places:
- the timetable, for the next hour: scheduled time at home, adjusted by the
  train's current delay when it's being tracked live;
- live positions, for trains already on a stretch through home.
"""

from collections.abc import Callable
from datetime import date, timedelta

from .railways import fraction_along
from .services import ServiceInfo
from .stations import Station
from .timetable import Schedule, Timetable, run_seconds_to_ts

LatLon = tuple[float, float]
# How close a stretch of track must come to the home point to count as passing it.
ON_LINE_KM = 0.25
# Straight-line stand-ins for missing track are rougher.
ON_STRAIGHT_KM = 0.6
# A stretch that only touches the home point at one end counts only if home is right at that
# station; otherwise every line converging on a nearby station would match.
AT_STATION_KM = 0.05


class HomeLine:
    def __init__(self, point: LatLon, track: Callable[[Station, Station], list[LatLon] | None],
                 info: ServiceInfo | None, stations: dict[str, Station]):
        self.point = point
        self.track = track
        self.info = info
        self.stations = stations
        self._fractions: dict[tuple[str, str], float | None] = {}
        self._scheduled: dict[tuple[date, int | None], list] = {}

    def fraction(self, a: Station, b: Station) -> float | None:
        """How far from a to b the home point is, if the a→b route passes it."""
        key = (a.crs, b.crs)
        if key not in self._fractions:
            path = self.track(a, b)
            limit = ON_LINE_KM if path else ON_STRAIGHT_KM
            path = path or [(a.lat, a.lon), (b.lat, b.lon)]
            f, off = fraction_along(path, *self.point)
            through = 0 < f < 1 and off <= limit
            # Home level with a station: trains stopping there count (their stretch ends there).
            at_station = f in (0.0, 1.0) and off <= AT_STATION_KM
            self._fractions[key] = f if through or at_station else None
        return self._fractions[key]

    def station(self, tiploc: str) -> Station | None:
        crs = self.info.crs_of.get(tiploc) if self.info else None
        return self.stations.get(crs) if crs else None

    def scheduled_pass(self, s: Schedule) -> tuple[int, Station, Station] | None:
        """(seconds after midnight at home, station before, station after) for one schedule."""
        located = [(c, st) for c in s.calls if c.time is not None and (st := self.station(c.tiploc))]
        for (ca, sa), (cb, sb) in zip(located, located[1:]):
            if sa.crs == sb.crs:
                continue
            f = self.fraction(sa, sb)
            if f is not None:
                leave = ca.time
                reach = cb.arrival if cb.arrival is not None else cb.time
                return round(leave + f * (reach - leave)), sa, sb
        return None

    def scheduled(self, tt: Timetable, run_date: date) -> list:
        """Every train due past home on a date: (unix time, schedule, before, after, planned cancelled)."""
        key = (run_date, tt.sequence)
        if key not in self._scheduled:
            rows = []
            for s, cancelled in tt.running_on(run_date):
                if s.bus:
                    continue
                hit = self.scheduled_pass(s)
                if hit:
                    seconds, a, b = hit
                    rows.append((run_seconds_to_ts(run_date, seconds), s, a, b, cancelled))
            rows.sort(key=lambda r: r[0])
            self._scheduled = {k: v for k, v in self._scheduled.items() if k[0] >= run_date - timedelta(days=1)}
            self._scheduled[key] = rows
        return self._scheduled[key]
