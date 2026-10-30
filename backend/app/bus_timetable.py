"""Bus timetables (BODS GTFS) for delays and next stops.

BODS publishes every bus timetable in England as a GTFS zip per region, regenerated
regularly and needing no API key. The South East's is ~300 MB (stop_times.txt alone is
~900 MB unzipped), so it's fetched weekly, filtered to the trips that pass through the
area around home, and kept as a small file (data/bus_timetable.json.gz).

A live bus is matched to its trip by operator, route number, first stop and the time it
was due to leave it (all in SIRI-VM). Its position along that trip's stops then gives its
delay: when it reported, minus when the timetable has it at that point.

Some operators (Stagecoach and First, around Guildford) also publish each route's path
along the roads (shapes.txt); those are kept too, simplified, to draw a bus's route.
"""

import csv
import gzip
import io
import json
import logging
import math
import time
import zipfile
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .geo import haversine_km

log = logging.getLogger(__name__)

UK = ZoneInfo("Europe/London")
GTFS_URL = "https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/{region}/"
# BODS's regions and roughly where each is centred, for choosing one from home.
REGIONS = {
    "south_east": (51.25, -0.75),
    "london": (51.51, -0.12),
    "south_west": (50.85, -3.4),
    "east_anglia": (52.3, 0.8),
    "east_midlands": (52.95, -1.0),
    "west_midlands": (52.5, -2.1),
    "north_west": (53.75, -2.6),
    "north_east": (54.85, -1.65),
    "yorkshire": (53.85, -1.3),
}
MAX_AGE_S = 7 * 86400
# Trips are kept if they pass within the bus radius plus this, so buses entering it are known.
MARGIN_KM = 5
FILE = "bus_timetable.json.gz"
# A bus further than this from its matched trip's route has been diverted or mismatched.
OFF_ROUTE_M = 1000
# Within this of a stop counts as being at it.
AT_STOP_M = 50
# A worked-out delay bigger than this either way is more likely a wrong match or placement
# (e.g. a circular route's shared start and end) than real, so it isn't shown.
MAX_PLAUSIBLE_S = 20 * 60
# Buses often announce their next journey while still at the terminus.
EARLY_ANNOUNCE_S = 60
UPCOMING = 5
# Bumped when the saved file gains something, so an older one is rebuilt (and used meanwhile).
FORMAT = 2
# Route lines are simplified to within this many metres of the published path.
SHAPE_TOLERANCE_M = 8


def pick_region(lat: float, lon: float) -> str:
    return min(REGIONS, key=lambda r: haversine_km(lat, lon, *REGIONS[r]))


def _seconds(hms: str) -> int:
    """GTFS times can pass 24:00:00 for journeys that run after midnight."""
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


# ---------- building the area timetable from the GTFS zip (slow: run in a thread) ----------

def build(zip_path: Path, lat: float, lon: float, radius_km: float) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        def rows(name):
            with z.open(name) as f:
                yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))

        reach = radius_km + MARGIN_KM
        area_stops = set()
        for r in rows("stops.txt"):
            try:
                if haversine_km(lat, lon, float(r["stop_lat"]), float(r["stop_lon"])) <= reach:
                    area_stops.add(r["stop_id"])
            except ValueError:
                continue
        # Two passes over stop_times: which trips touch the area, then their whole stop lists.
        wanted = {r["trip_id"] for r in rows("stop_times.txt") if r["stop_id"] in area_stops}
        calls: dict[str, list] = {}
        for r in rows("stop_times.txt"):
            if r["trip_id"] in wanted:
                try:
                    calls.setdefault(r["trip_id"], []).append(
                        (int(r["stop_sequence"]), r["stop_id"], _seconds(r["arrival_time"]),
                         _seconds(r["departure_time"])))
                except (ValueError, KeyError):
                    continue
        used_stops = {c[1] for cs in calls.values() for c in cs}
        stops = {}
        for r in rows("stops.txt"):
            if r["stop_id"] in used_stops:
                try:
                    stops[r["stop_id"]] = [r["stop_name"], round(float(r["stop_lat"]), 6), round(float(r["stop_lon"]), 6)]
                except ValueError:
                    continue
        nocs = {r["agency_id"]: r.get("agency_noc") or r["agency_id"] for r in rows("agency.txt")}
        routes = {r["route_id"]: [nocs.get(r["agency_id"]), r["route_short_name"]] for r in rows("routes.txt")}
        trips = {}
        for r in rows("trips.txt"):
            stop_list = calls.get(r["trip_id"])
            if not stop_list or r["route_id"] not in routes:
                continue
            noc, line = routes[r["route_id"]]
            stop_list.sort()
            trips[r["trip_id"]] = [noc, line, r["service_id"], r.get("trip_headsign") or "",
                                   [[c[1], c[2], c[3]] for c in stop_list if c[1] in stops],
                                   r.get("shape_id") or None]
        shapes = _shapes(rows, {t[5] for t in trips.values() if t[5]})
        for t in trips.values():
            if t[5] not in shapes:
                t[5] = None
        services = {t[2] for t in trips.values()}
        calendar = {r["service_id"]: [r["monday"] + r["tuesday"] + r["wednesday"] + r["thursday"] + r["friday"]
                                      + r["saturday"] + r["sunday"], r["start_date"], r["end_date"]]
                    for r in rows("calendar.txt") if r["service_id"] in services}
        exceptions: dict[str, dict] = {}
        for r in rows("calendar_dates.txt"):
            if r["service_id"] in services:
                exceptions.setdefault(r["service_id"], {})[r["date"]] = r["exception_type"] == "1"
        try:
            version = next(rows("feed_info.txt")).get("feed_version")
        except (KeyError, StopIteration):
            version = None
    return {"version": version, "stops": stops, "trips": trips, "calendar": calendar, "exceptions": exceptions,
            "shapes": shapes}


def _shapes(rows, wanted: set[str]) -> dict[str, list]:
    """The wanted route paths from shapes.txt, as simplified [lat, lon] lists."""
    points: dict[str, list] = {}
    if not wanted:
        return {}
    try:
        for r in rows("shapes.txt"):
            if r["shape_id"] in wanted:
                try:
                    points.setdefault(r["shape_id"], []).append(
                        (int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))
                except (ValueError, KeyError):
                    continue
    except KeyError:  # no shapes.txt in this feed
        return {}
    return {sid: simplify([(p[1], p[2]) for p in sorted(pts)]) for sid, pts in points.items() if len(pts) >= 2}


def simplify(line: list[tuple[float, float]], tolerance_m: float = SHAPE_TOLERANCE_M) -> list[list[float]]:
    """Douglas-Peucker: drop points the line passes within `tolerance_m` of anyway."""
    if len(line) < 3:
        return [[round(a, 5), round(b, 5)] for a, b in line]
    lat0 = line[0][0]
    xy = [_xy(a, b, lat0) for a, b in line]
    keep = [False] * len(line)
    keep[0] = keep[-1] = True
    stack = [(0, len(line) - 1)]
    while stack:
        i, j = stack.pop()
        (ax, ay), (bx, by) = xy[i], xy[j]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        far, far_d = None, tolerance_m
        for k in range(i + 1, j):
            px, py = xy[k]
            d = (abs(dx * (ay - py) - dy * (ax - px)) / length) if length else math.hypot(px - ax, py - ay)
            if d > far_d:
                far, far_d = k, d
        if far is not None:
            keep[far] = True
            stack += [(i, far), (far, j)]
    return [[round(a, 5), round(b, 5)] for (a, b), k in zip(line, keep) if k]


# ---------- using it ----------

def _xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Metres on a flat local grid (fine over a bus route's few km)."""
    return lon * 111_320 * math.cos(math.radians(lat0)), lat * 110_540


class BusTimetable:
    def __init__(self, data: dict, built: float, region: str, area: str, format: int = FORMAT):
        self.version = data.get("version")
        self.format = format
        self.stops = data["stops"]
        self.trips = data["trips"]
        self.shapes = data.get("shapes", {})
        self.calendar = data["calendar"]
        self.exceptions = data["exceptions"]
        self.built, self.region, self.area = built, region, area
        self._indexes: dict[date, dict] = {}
        # Last segment each vehicle was found on, so a route that doubles back is read in order.
        self._progress: dict[str, tuple[str, int]] = {}

    # --- files ---

    @classmethod
    def load(cls, path: Path) -> "BusTimetable | None":
        try:
            with gzip.open(path, "rt") as f:
                saved = json.load(f)
            return cls(saved["data"], saved["built"], saved["region"], saved["area"], saved.get("format", 1))
        except (OSError, ValueError, KeyError):
            return None

    @staticmethod
    def save(path: Path, data: dict, region: str, area: str, built: float) -> None:
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as f:
            json.dump({"data": data, "region": region, "area": area, "built": built, "format": FORMAT}, f,
                      separators=(",", ":"))
        tmp.replace(path)

    # --- which trips run when ---

    def _runs(self, service_id: str, day: date) -> bool:
        ymd = day.strftime("%Y%m%d")
        override = self.exceptions.get(service_id, {}).get(ymd)
        if override is not None:
            return override
        cal = self.calendar.get(service_id)
        return bool(cal) and cal[1] <= ymd <= cal[2] and cal[0][day.weekday()] == "1"

    def _index(self, day: date) -> dict:
        """(operator, route, first stop, seconds after midnight) -> trip, for trips running on `day`."""
        if day not in self._indexes:
            self._indexes = {d: i for d, i in self._indexes.items() if abs((d - day).days) <= 1}
            index = {}
            for trip_id, (noc, line, service, _, calls, *_) in self.trips.items():
                if calls and self._runs(service, day):
                    index[(noc, line, calls[0][0], calls[0][2])] = trip_id
            self._indexes[day] = index
        return self._indexes[day]

    def match(self, bus: dict) -> tuple[str, date] | None:
        if not (bus.get("origin_ref") and bus.get("origin_departure")):
            return None
        try:
            day = date.fromisoformat(bus["service_date"]) if bus.get("service_date") else None
        except ValueError:
            day = None
        departs = datetime.fromtimestamp(bus["origin_departure"], UK)
        # Some operators send a placeholder service date (0001-01-01): only trust a plausible one.
        if day and abs((day - departs.date()).days) > 1:
            day = None
        for d in ([day] if day else [departs.date(), departs.date() - timedelta(days=1)]):
            seconds = int(bus["origin_departure"] - datetime.combine(d, dtime(0), UK).timestamp())
            trip = self._index(d).get((bus["operator"], bus["line"], bus["origin_ref"], seconds))
            if trip:
                return trip, d
        return None

    # --- where a bus is on its trip ---

    def progress(self, bus: dict) -> dict | None:
        """Delay and next stops for a live bus, or None if it can't be matched or placed."""
        found = self.match(bus)
        if not found:
            return None
        trip_id, day = found
        noc, line, _, headsign, calls, *_ = self.trips[trip_id]
        points = [(self.stops[c[0]], c) for c in calls]
        if len(points) < 2:
            return None
        midnight = datetime.combine(day, dtime(0), UK).timestamp()
        now = bus["recorded"] - midnight  # seconds after the service day's midnight
        here = _xy(bus["lat"], bus["lon"], bus["lat"])

        def stop_info(i: int, delay: float) -> dict:
            due = points[i][1][1]
            # Late buses are assumed to stay late; early ones to wait at stops and be on time.
            return {"name": points[i][0][0], "due": midnight + due, "expected": midnight + due + max(delay, 0)}

        if now < calls[0][2] - EARLY_ANNOUNCE_S:
            # The journey hasn't started: the bus is showing it while it waits (or finishes the last).
            return {"trip_id": trip_id, "trip_headsign": headsign or None, "delay_s": None, "at_stop": None,
                    "starts_at": midnight + calls[0][2], "first_stop": points[0][0][0], "finished": False,
                    "next_stops": [stop_info(i, 0) for i in range(min(UPCOMING, len(points)))]}

        # Nearest stretch between consecutive stops. Routes that double back pass the same road
        # twice, so among stretches about as near, take the one the timetable has the bus on
        # about now, and never one behind where this bus was last time.
        def due_at(k: int, t: float) -> float:
            leave, reach = points[k][1][2], points[k + 1][1][1]
            return leave + t * (reach - leave)

        candidates = []
        for k in range(len(points) - 1):
            a = _xy(points[k][0][1], points[k][0][2], bus["lat"])
            b = _xy(points[k + 1][0][1], points[k + 1][0][2], bus["lat"])
            dx, dy = b[0] - a[0], b[1] - a[1]
            length2 = dx * dx + dy * dy
            t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((here[0] - a[0]) * dx + (here[1] - a[1]) * dy) / length2))
            dist = math.hypot(here[0] - (a[0] + t * dx), here[1] - (a[1] + t * dy))
            candidates.append((dist, k, t))
        best = min(c[0] for c in candidates)
        if best > OFF_ROUTE_M:
            return None
        previous = self._progress.get(bus["id"])
        floor = previous[1] if previous and previous[0] == trip_id else 0
        close = [c for c in candidates if c[0] <= best + 100]
        dist, k, t = min(close, key=lambda c: (c[1] < floor, abs(now - due_at(c[1], c[2]))))
        self._progress[bus["id"]] = (trip_id, k)

        def metres_to(i: int) -> float:
            stop = _xy(points[i][0][1], points[i][0][2], bus["lat"])
            return math.hypot(here[0] - stop[0], here[1] - stop[1])

        delay = now - due_at(k, t)
        # At a stop: the one it's reached (the end of its stretch) or the one it's leaving.
        stop = k + 1 if metres_to(k + 1) <= AT_STOP_M else k if metres_to(k) <= AT_STOP_M else None
        if stop is not None:
            delay = now - points[stop][1][1]
            if now <= points[stop][1][2]:
                delay = 0  # waiting at a stop until it's time to go: on time
        if abs(delay) > MAX_PLAUSIBLE_S:
            return None
        last = len(points) - 1
        finished = stop == last
        after = stop + 1 if stop is not None else k + 1
        return {
            "trip_id": trip_id,
            "trip_headsign": headsign or None,
            "delay_s": round(delay),
            "at_stop": points[stop][0][0] if stop is not None else None,
            "starts_at": None,
            "first_stop": None,
            "finished": finished,
            "next_stops": [stop_info(i, delay) for i in range(after, min(after + UPCOMING, len(points)))],
        }


    def route(self, bus: dict) -> dict | None:
        """A matched bus's whole route, to draw: the road path when published, and every stop."""
        found = self.match(bus)
        if not found:
            return None
        trip_id, day = found
        _, _, _, _, calls, *rest = self.trips[trip_id]
        shape = self.shapes.get(rest[0]) if rest and rest[0] else None
        midnight = datetime.combine(day, dtime(0), UK).timestamp()
        stops = [{"name": self.stops[c[0]][0], "lat": self.stops[c[0]][1], "lon": self.stops[c[0]][2],
                  "due": midnight + c[1]} for c in calls]
        return {"trip_id": trip_id, "follows_roads": shape is not None,
                "line": shape or [[s["lat"], s["lon"]] for s in stops], "stops": stops}


# ---------- keeping it up to date ----------

async def download(client: httpx.AsyncClient, region: str, dest: Path) -> None:
    """Stream the region's GTFS zip to `dest` (hundreds of MB)."""
    tmp = dest.with_suffix(".part")
    async with client.stream("GET", GTFS_URL.format(region=region), timeout=httpx.Timeout(60, read=300),
                             follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            async for chunk in resp.aiter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(dest)


def area_key(lat: float, lon: float, radius_km: float) -> str:
    return f"{lat:.3f},{lon:.3f},{radius_km}"


def stale(table: "BusTimetable | None", region: str, area: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return (table is None or table.format != FORMAT or table.region != region or table.area != area
            or now - table.built > MAX_AGE_S)
