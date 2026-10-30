"""Railway track geometry from OpenStreetMap, for drawing lines and routing trains.

Track around home is fetched once from the Overpass API and cached. Shared
OSM nodes join the ways into a graph, so a train between two stations can be
placed along the actual track instead of on a straight line.

Data © OpenStreetMap contributors, ODbL.
"""

import heapq
import json
import logging
import math
import time
from pathlib import Path

import httpx

from ..geo import haversine_km
from .stations import Station

log = logging.getLogger(__name__)

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
MAX_AGE = 30 * 86400
# Fetch beyond the train radius so the next station of a train leaving the area is covered.
MARGIN_KM = 10
# A station further than this from any track node isn't on the network we have.
MAX_SNAP_KM = 0.6
# Reject routes much longer than the straight line: they're detours, not the real path.
MAX_DETOUR = 2.5

LatLon = tuple[float, float]


def _usable(tags: dict) -> bool:
    # Sidings and yards create shortcuts trains don't take; heritage lines aren't on TRUST.
    return tags.get("service") not in ("siding", "yard", "spur") and tags.get("usage") != "tourism"


class RailNetwork:
    def __init__(self, elements: list[dict]):
        self.coords: dict[int, LatLon] = {}
        self.graph: dict[int, list[tuple[int, float]]] = {}
        self.lines: list[list[LatLon]] = []
        for way in elements:
            nodes, geometry = way.get("nodes") or [], way.get("geometry") or []
            if way.get("type") != "way" or len(nodes) != len(geometry) or not _usable(way.get("tags", {})):
                continue
            points = [(round(g["lat"], 6), round(g["lon"], 6)) for g in geometry]
            if way.get("tags", {}).get("service") != "crossover":
                self.lines.append(points)
            for node, point in zip(nodes, points):
                self.coords[node] = point
            for a, b in zip(nodes, nodes[1:]):
                d = haversine_km(*self.coords[a], *self.coords[b])
                self.graph.setdefault(a, []).append((b, d))
                self.graph.setdefault(b, []).append((a, d))
        self._snap: dict[str, int | None] = {}
        self._paths: dict[tuple[str, str], list[LatLon] | None] = {}

    def nearest_node(self, station: Station) -> int | None:
        if station.crs not in self._snap:
            best, best_d = None, MAX_SNAP_KM
            for node, (lat, lon) in self.coords.items():
                # Cheap bounding check before the full distance.
                if abs(lat - station.lat) > 0.01 or abs(lon - station.lon) > 0.015:
                    continue
                d = haversine_km(station.lat, station.lon, lat, lon)
                if d < best_d:
                    best, best_d = node, d
            self._snap[station.crs] = best
        return self._snap[station.crs]

    def path(self, a: Station, b: Station) -> list[LatLon] | None:
        """Track from station a to station b, or None if it isn't in the network."""
        key = (a.crs, b.crs)
        if key not in self._paths:
            reverse = self._paths.get((b.crs, a.crs))
            if reverse is not None:
                self._paths[key] = reverse[::-1]
            else:
                self._paths[key] = self._route(a, b)
        return self._paths[key]

    def _route(self, a: Station, b: Station) -> list[LatLon] | None:
        start, goal = self.nearest_node(a), self.nearest_node(b)
        if start is None or goal is None or start == goal:
            return None
        straight = haversine_km(a.lat, a.lon, b.lat, b.lon)
        limit = max(straight * MAX_DETOUR, straight + 1)
        dist, prev = {start: 0.0}, {}
        queue = [(0.0, start)]
        while queue:
            d, node = heapq.heappop(queue)
            if node == goal:
                break
            if d > dist.get(node, math.inf) or d > limit:
                continue
            for nxt, w in self.graph.get(node, ()):
                nd = d + w
                if nd < dist.get(nxt, math.inf):
                    dist[nxt], prev[nxt] = nd, node
                    heapq.heappush(queue, (nd, nxt))
        if goal not in prev or dist[goal] > limit:
            return None
        nodes = [goal]
        while nodes[-1] != start:
            nodes.append(prev[nodes[-1]])
        return [self.coords[n] for n in reversed(nodes)]

    def as_dict(self) -> dict:
        return {"lines": self.lines}


def _cumulative(path: list[LatLon]) -> list[float]:
    total = [0.0]
    for p, q in zip(path, path[1:]):
        total.append(total[-1] + haversine_km(*p, *q))
    return total


def point_along(path: list[LatLon], f: float) -> LatLon:
    """The point a fraction f (0–1) of the way along a polyline, by distance."""
    cum = _cumulative(path)
    target = min(max(f, 0.0), 1.0) * cum[-1]
    for i in range(1, len(path)):
        if cum[i] >= target:
            seg = cum[i] - cum[i - 1]
            t = (target - cum[i - 1]) / seg if seg else 0.0
            (lat1, lon1), (lat2, lon2) = path[i - 1], path[i]
            return lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1)
    return path[-1]


def fraction_along(path: list[LatLon], lat: float, lon: float) -> tuple[float, float]:
    """(fraction along the polyline, distance in km) of its closest point to (lat, lon)."""
    cum = _cumulative(path)
    best = (math.inf, 0.0)
    k = math.cos(math.radians(lat))
    for i in range(1, len(path)):
        (lat1, lon1), (lat2, lon2) = path[i - 1], path[i]
        dx, dy = (lon2 - lon1) * k, lat2 - lat1
        px, py = (lon - lon1) * k, lat - lat1
        seg_sq = dx * dx + dy * dy
        t = min(max((px * dx + py * dy) / seg_sq, 0.0), 1.0) if seg_sq else 0.0
        clat, clon = lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1)
        d = haversine_km(lat, lon, clat, clon)
        if d < best[0]:
            best = (d, cum[i - 1] + t * (cum[i] - cum[i - 1]))
    total = cum[-1] or 1.0
    return best[1] / total, best[0]


async def overpass(client: httpx.AsyncClient, query: str, what: str) -> list[dict]:
    """Run an Overpass query, trying each server in turn (the main one is often busy)."""
    last_error: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            log.info("Downloading %s from OpenStreetMap (%s)", what, url)
            resp = await client.post(url, data={"data": query}, timeout=180)
            resp.raise_for_status()
            return resp.json()["elements"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            last_error = exc
            log.warning("Overpass server %s failed: %s", url, exc)
    raise RuntimeError(f"no OpenStreetMap server answered: {last_error}")


async def load_railways(
    client: httpx.AsyncClient, data_dir: Path, lat: float, lon: float, radius_km: float
) -> RailNetwork:
    path = data_dir / "railways.json"
    query_key = f"{lat:.4f},{lon:.4f},{radius_km}"
    cached = json.loads(path.read_text()) if path.exists() else None
    fresh = cached and cached.get("key") == query_key and time.time() - cached.get("fetched", 0) < MAX_AGE
    if not fresh:
        radius_m = int((radius_km + MARGIN_KM) * 1000)
        query = f'[out:json][timeout:120];way["railway"="rail"](around:{radius_m},{lat},{lon});out body geom;'
        try:
            elements = await overpass(client, query, "railway lines")
            cached = {"key": query_key, "fetched": time.time(), "elements": elements}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cached))
        except RuntimeError:
            if not cached:
                raise
            log.warning("Using cached railway lines from a previous download")
    return RailNetwork(cached["elements"])
