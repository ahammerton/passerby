"""Where a satellite is, and when it passes over home.

Positions come from SGP4 (the `sgp4` library) using CelesTrak's orbital elements. The rest is
plain geometry, accurate to a fraction of a degree, which is plenty for "look south-west":
- Earth rotation: Greenwich mean sidereal time (TEME to Earth-fixed, ignoring polar motion).
- The Sun: the Astronomical Almanac's low-precision formula (about 0.01°).
- Earth's shadow: a cylinder, so a satellite is lit unless it's behind the Earth from the Sun.

A pass is visible to the eye when the satellite is sunlit, the sky at home is dark enough
(the Sun more than 6° below the horizon) and it's at least 10° up.
"""

import math
from dataclasses import dataclass

from sgp4.api import Satrec, jday

EARTH_RADIUS_KM = 6378.137
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)

VISIBLE_MIN_ELEVATION = 10.0  # lower than this, buildings and haze usually hide it
DARK_SKY_SUN_ELEVATION = -6.0  # civil twilight: dark enough to see bright satellites
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg: float) -> str:
    return COMPASS[round(deg / 22.5) % 16]


def julian(t: float) -> tuple[float, float]:
    """Unix time as the (day, fraction) pair SGP4 takes."""
    jd = t / 86400.0 + 2440587.5
    whole = math.floor(jd - 0.5) + 0.5
    return whole, jd - whole


def gmst_rad(t: float) -> float:
    d = t / 86400.0 + 2440587.5 - 2451545.0
    return math.radians((280.46061837 + 360.98564736629 * d) % 360.0)


def teme_to_ecef(r: tuple[float, float, float], t: float) -> tuple[float, float, float]:
    g = gmst_rad(t)
    c, s = math.cos(g), math.sin(g)
    return (c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2])


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """(latitude°, longitude°, height km) on the WGS84 ellipsoid."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS84_E2))
    for _ in range(5):
        n = EARTH_RADIUS_KM / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
    n = EARTH_RADIUS_KM / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    h = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), h


def geodetic_to_ecef(lat: float, lon: float, h_km: float = 0.0) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    n = EARTH_RADIUS_KM / math.sqrt(1 - WGS84_E2 * math.sin(la) ** 2)
    return ((n + h_km) * math.cos(la) * math.cos(lo), (n + h_km) * math.cos(la) * math.sin(lo),
            (n * (1 - WGS84_E2) + h_km) * math.sin(la))


def sun_eci(t: float) -> tuple[float, float, float]:
    """Unit vector towards the Sun, in the same (equator of date) frame as TEME, near enough."""
    n = t / 86400.0 + 2440587.5 - 2451545.0
    mean_lon = math.radians((280.460 + 0.9856474 * n) % 360)
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    ecl_lon = mean_lon + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    return (math.cos(ecl_lon), math.cos(eps) * math.sin(ecl_lon), math.sin(eps) * math.sin(ecl_lon))


class Observer:
    """Home: works out where something is in the sky from there."""

    def __init__(self, lat: float, lon: float, height_km: float = 0.05):
        self.lat, self.lon = lat, lon
        self.ecef = geodetic_to_ecef(lat, lon, height_km)
        la, lo = math.radians(lat), math.radians(lon)
        self._sl, self._cl, self._so, self._co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)

    def look(self, target_ecef: tuple[float, float, float]) -> tuple[float, float, float]:
        """(azimuth°, elevation°, range km) of an Earth-fixed point."""
        dx, dy, dz = (target_ecef[i] - self.ecef[i] for i in range(3))
        east = -self._so * dx + self._co * dy
        north = -self._sl * self._co * dx - self._sl * self._so * dy + self._cl * dz
        up = self._cl * self._co * dx + self._cl * self._so * dy + self._sl * dz
        rng = math.sqrt(east * east + north * north + up * up)
        return math.degrees(math.atan2(east, north)) % 360, math.degrees(math.asin(up / rng)), rng

    def sun_elevation(self, t: float) -> float:
        s = teme_to_ecef(sun_eci(t), t)
        far = tuple(c * 1.496e8 for c in s)
        return self.look(far)[1]


def sunlit(r_teme: tuple[float, float, float], t: float) -> bool:
    s = sun_eci(t)
    along = sum(r_teme[i] * s[i] for i in range(3))
    if along > 0:
        return True  # on the Sun's side of the Earth
    perp = math.sqrt(max(sum(c * c for c in r_teme) - along * along, 0.0))
    return perp > EARTH_RADIUS_KM


@dataclass
class Satellite:
    norad: int
    name: str
    line1: str
    line2: str

    def __post_init__(self):
        self.satrec = Satrec.twoline2rv(self.line1, self.line2)

    @property
    def epoch(self) -> float:
        """When the elements were measured (unix time)."""
        return (self.satrec.jdsatepoch + self.satrec.jdsatepochF - 2440587.5) * 86400.0

    def teme(self, t: float) -> tuple[float, float, float] | None:
        jd, fr = julian(t)
        err, r, _ = self.satrec.sgp4(jd, fr)
        return None if err else r

    def where(self, t: float) -> dict | None:
        """Sub-satellite point and height."""
        r = self.teme(t)
        if r is None:
            return None
        lat, lon, h = ecef_to_geodetic(*teme_to_ecef(r, t))
        return {"lat": round(lat, 4), "lon": round(lon, 4), "alt_km": round(h, 1)}


def parse_tle(text: str) -> list[Satellite]:
    """Three-line element sets (name, line 1, line 2), skipping anything malformed."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out = []
    i = 0
    while i + 2 < len(lines):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            try:
                out.append(Satellite(int(l1[2:7]), name.strip(), l1, l2))
            except ValueError:
                pass
            i += 3
        else:
            i += 1
    return out


def _sky(sat: Satellite, obs: Observer, t: float):
    r = sat.teme(t)
    if r is None:
        return None
    az, el, rng = obs.look(teme_to_ecef(r, t))
    return r, az, el, rng


def _visible_at(sat: Satellite, obs: Observer, t: float) -> bool:
    s = _sky(sat, obs, t)
    return (s is not None and s[2] >= VISIBLE_MIN_ELEVATION and sunlit(s[0], t)
            and obs.sun_elevation(t) <= DARK_SKY_SUN_ELEVATION)


def _crossing(sat: Satellite, obs: Observer, a: float, b: float) -> float:
    """When the elevation crosses 0° between a and b (one up, one down), to a second."""
    ea = _sky(sat, obs, a)[2]
    while b - a > 1:
        m = (a + b) / 2
        em = _sky(sat, obs, m)[2]
        if (em > 0) == (ea > 0):
            a, ea = m, em
        else:
            b = m
    return (a + b) / 2


def _point(sat: Satellite, obs: Observer, t: float) -> dict:
    _, az, el, rng = _sky(sat, obs, t)
    return {"time": round(t), "az": round(az), "dir": compass(az), "el": round(el), "range_km": round(rng)}


def passes(sat: Satellite, obs: Observer, start: float, hours: float, step: float = 30.0,
           min_el: float = 0.0) -> list[dict]:
    """Every pass above the horizon in the window, with when it's visible to the eye if it is."""
    out = []
    end = start + hours * 3600
    prev_t, prev = start, _sky(sat, obs, start)
    if prev is None:
        return out
    rise = start if prev[2] > 0 else None
    t = start
    while t < end:
        t += step
        cur = _sky(sat, obs, t)
        if cur is None:
            return out
        if prev[2] <= 0 < cur[2]:
            rise = _crossing(sat, obs, prev_t, t)
        elif prev[2] > 0 >= cur[2] and rise is not None:
            p = _describe(sat, obs, rise, _crossing(sat, obs, prev_t, t))
            if p["max"]["el"] >= min_el:
                out.append(p)
            rise = None
        prev_t, prev = t, cur
    return out


def _describe(sat: Satellite, obs: Observer, rise: float, set_: float) -> dict:
    # The highest point, then the visible stretch, sampled every 5 s (passes last minutes).
    samples = []
    t = rise
    while t <= set_:
        samples.append(t)
        t += 5
    samples.append(set_)
    peak = max(samples, key=lambda x: _sky(sat, obs, x)[2])
    seen = [x for x in samples if _visible_at(sat, obs, x)]
    p = {
        "norad": sat.norad, "name": sat.name,
        "rise": _point(sat, obs, rise), "max": _point(sat, obs, peak), "set": _point(sat, obs, set_),
        "visible": bool(seen),
    }
    if not seen:
        # Why you can't see it: always low, the sky still light, or the satellite in Earth's shadow.
        if p["max"]["el"] < VISIBLE_MIN_ELEVATION:
            p["why_not"] = "too low"
        elif obs.sun_elevation(peak) > DARK_SKY_SUN_ELEVATION:
            p["why_not"] = "daylight"
        else:
            p["why_not"] = "shadow"
    if seen:
        appears, vanishes = seen[0], seen[-1]
        p["appears"] = _point(sat, obs, appears)
        p["vanishes"] = _point(sat, obs, vanishes)
        # Why it disappears early, if it does: into Earth's shadow, or just over the horizon.
        r = sat.teme(vanishes + 5)
        p["vanishes"]["into_shadow"] = vanishes < set_ - 5 and r is not None and not sunlit(r, vanishes + 5)
        # The highest point you can actually see (the pass may go higher in Earth's shadow).
        top = max(seen, key=lambda x: _sky(sat, obs, x)[2])
        p["visible_peak"] = _point(sat, obs, top)
        p["visible_max_el"] = p["visible_peak"]["el"]
    return p


def ground_track(sat: Satellite, start: float, minutes: float, step_s: float = 30.0) -> list[list[float]]:
    """[[time, lat, lon], ...] for drawing the path and moving the marker smoothly between updates."""
    out = []
    t = start
    while t <= start + minutes * 60:
        w = sat.where(t)
        if w:
            out.append([round(t), w["lat"], w["lon"]])
        t += step_s
    return out
