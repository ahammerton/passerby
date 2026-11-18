"""Space stations and bright satellites passing over home.

Orbits come from CelesTrak (https://celestrak.org), free and without a key. CelesTrak asks
that each file is downloaded no more than once every two hours; Passerby keeps a copy in
data/ and refreshes it twice a day (station orbits change after every reboost).
"""

import asyncio
import logging
import time
from pathlib import Path

import httpx

from .orbit import Observer, Satellite, ground_track, julian, parse_tle, passes, teme_to_ecef

log = logging.getLogger("passerby")

CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP={}&FORMAT=tle"
GROUPS = ("stations", "visual")  # crewed stations and their modules; the ~150 brightest satellites
REFRESH_SECONDS = 12 * 3600
RETRY_SECONDS = 2 * 3600  # CelesTrak's minimum gap between downloads of one file

# Space stations, by catalogue number: each station's core module (the others travel with it).
STATIONS = {
    25544: {"name": "International Space Station", "short": "ISS"},
    48274: {"name": "Tiangong (China's space station)", "short": "Tiangong"},
}

STATION_HOURS = 72  # passes worked out this far ahead for stations…
SATELLITE_HOURS = 24  # …and this far for bright satellites
SATELLITE_MIN_EL = 30
SATELLITE_LIST_HOURS = 12  # only satellites passing well up count: low ones are hard to spot
TRACK_BEHIND_MIN, TRACK_AHEAD_MIN = 10, 95  # about one orbit ahead
ALERT_LEAD_SECONDS = 5 * 60
ALERT_LEVELS = ("stations", "off")


async def load_group(client: httpx.AsyncClient, data_dir: Path, group: str, now: float | None = None) -> str | None:
    """A group's elements: the cached copy if fresh, else downloaded (falling back to the cache)."""
    now = time.time() if now is None else now
    path = data_dir / f"tle_{group}.txt"
    age = now - path.stat().st_mtime if path.exists() else None
    if age is not None and age < REFRESH_SECONDS:
        return path.read_text()
    try:
        resp = await client.get(CELESTRAK.format(group), timeout=30)
        resp.raise_for_status()
        text = resp.text
        if not parse_tle(text):
            raise ValueError("no orbital elements in the response")
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return text
    except Exception as exc:
        log.warning("CelesTrak %s download failed: %s", group, exc)
        return path.read_text() if path.exists() else None


class SpaceTracker:
    def __init__(self, home_lat: float, home_lon: float, alerts: str = "stations"):
        self.observer = Observer(home_lat, home_lon)
        self.alerts = alerts if alerts in ALERT_LEVELS else "stations"
        self.stations: dict[int, Satellite] = {}
        self.satellites: list[Satellite] = []
        self.station_passes: list[dict] = []
        self.satellite_passes: list[dict] = []
        self.status: dict = {"ok": None, "error": None, "updated": None, "satellites": 0}
        self._alerted: set[tuple[int, int]] = set()

    # ---------- orbits ----------

    def set_elements(self, stations_text: str | None, visual_text: str | None) -> None:
        stations = parse_tle(stations_text or "")
        self.stations = {s.norad: s for s in stations if s.norad in STATIONS}
        known = set(self.stations)
        self.satellites = [s for s in parse_tle(visual_text or "") if s.norad not in known]

    def compute_passes(self, now: float | None = None) -> None:
        """Slow (a second or two): run in a worker thread."""
        now = time.time() if now is None else now
        station_passes = []
        for norad, sat in self.stations.items():
            for p in passes(sat, self.observer, now - 600, STATION_HOURS):
                station_passes.append(p | {"station": STATIONS[norad]["short"]})
        satellite_passes = []
        for sat in self.satellites:
            satellite_passes += [p for p in passes(sat, self.observer, now - 600, SATELLITE_HOURS, step=60,
                                                   min_el=SATELLITE_MIN_EL) if p["visible"]]
        self.station_passes = sorted(station_passes, key=lambda p: p["rise"]["time"])
        self.satellite_passes = sorted(satellite_passes, key=lambda p: p["appears"]["time"])

    # ---------- for the browser ----------

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        stations = []
        for norad, sat in self.stations.items():
            here = sat.where(now)
            if here is None:
                continue
            az, el, rng = self.observer.look(_ecef(sat, now))
            upcoming = [p for p in self.station_passes if p["norad"] == norad and p["set"]["time"] >= now]
            stations.append({
                "norad": norad, **STATIONS[norad], **here,
                "az": round(az), "el": round(el, 1), "range_km": round(rng),
                "speed_kms": round(_speed(sat, now), 2),
                "elements_age_h": round((now - sat.epoch) / 3600, 1),
                "track": ground_track(sat, now - TRACK_BEHIND_MIN * 60, TRACK_BEHIND_MIN + TRACK_AHEAD_MIN),
                "passes": upcoming[:8],
            })
        # Every visible station pass; bright satellites only for the next 12 hours (there are dozens a day).
        visible = [p for p in self.station_passes if p["visible"] and p["vanishes"]["time"] >= now]
        visible += [p for p in self.satellite_passes
                    if p["vanishes"]["time"] >= now and p["appears"]["time"] < now + SATELLITE_LIST_HOURS * 3600]
        visible.sort(key=lambda p: p["appears"]["time"])
        return {"status": self.status, "stations": stations, "visible_passes": visible,
                "satellites": len(self.satellites)}

    def new_alerts(self, now: float | None = None) -> list[dict]:
        """Visible station passes starting within the next few minutes, once each."""
        if self.alerts == "off":
            return []
        now = time.time() if now is None else now
        due = []
        for p in self.station_passes:
            if not p["visible"]:
                continue
            key = (p["norad"], p["appears"]["time"])
            if key in self._alerted:
                continue
            if 0 <= p["appears"]["time"] - now <= ALERT_LEAD_SECONDS:
                self._alerted.add(key)
                due.append(p)
        self._alerted = {k for k in self._alerted if k[1] > now - 3600}
        return due


def _ecef(sat: Satellite, t: float):
    return teme_to_ecef(sat.teme(t), t)


def _speed(sat: Satellite, t: float) -> float:
    """Orbital speed, km/s."""
    _, _, v = sat.satrec.sgp4(*julian(t))
    return sum(c * c for c in v) ** 0.5


async def run(tracker: SpaceTracker, client: httpx.AsyncClient, data_dir: Path) -> None:
    """Keep the orbits fresh and the passes worked out."""
    loaded = 0.0
    while True:
        now = time.time()
        if now - loaded >= REFRESH_SECONDS or not tracker.stations:
            stations_text = await load_group(client, data_dir, "stations")
            visual_text = await load_group(client, data_dir, "visual")
            tracker.set_elements(stations_text, visual_text)
            if tracker.stations:
                loaded = now
                tracker.status.update(ok=True, error=None, updated=now, satellites=len(tracker.satellites))
                log.info("Space: %d stations, %d bright satellites", len(tracker.stations), len(tracker.satellites))
            else:
                tracker.status.update(ok=False, error="Couldn't get orbits from CelesTrak")
        if tracker.stations:
            started = time.time()
            await asyncio.to_thread(tracker.compute_passes)
            log.debug("Space passes worked out in %.1fs", time.time() - started)
        # Passes are good for hours; recheck every half hour (new elements, and to roll the window on).
        await asyncio.sleep(1800 if tracker.stations else RETRY_SECONDS)
