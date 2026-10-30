"""A station's physical layout from OpenStreetMap: numbered platforms and every track.

Network Rail's open data says which platform a train uses but not where the
platforms are, so the plan comes from OpenStreetMap (© OpenStreetMap
contributors, ODbL). Fetched on first view and cached per station.
"""

import json
import re
import time
from pathlib import Path

import httpx

from .railways import overpass
from .stations import Station

MAX_AGE = 30 * 86400
# Platforms and tracks this close to the station's point belong to it.
RADIUS_M = 500

LatLon = list[float]


def platform_refs(tags: dict) -> list[str]:
    """Platform numbers an OSM platform carries: "4;5" (an island platform) is both 4 and 5."""
    raw = tags.get("ref") or tags.get("local_ref") or ""
    if not raw:
        # Some are only named, e.g. "Platform 3".
        m = re.search(r"platforms?\s+([\w;,/ &-]+)", tags.get("name", ""), re.I)
        raw = m.group(1) if m else ""
    return [r.strip().upper() for r in re.split(r"[;,/&]|\s+and\s+", raw) if r.strip()]


def _lines(element: dict) -> list[list[LatLon]]:
    if element.get("type") == "way":
        return [[[round(p["lat"], 6), round(p["lon"], 6)] for p in element.get("geometry") or []]]
    # Relations (multipolygon platforms): their outer member ways.
    return [[[round(p["lat"], 6), round(p["lon"], 6)] for p in m.get("geometry") or []]
            for m in element.get("members") or [] if m.get("role") in ("outer", "") and m.get("geometry")]


def parse_layout(elements: list[dict]) -> dict:
    platforms, tracks, seen = [], [], set()
    for e in elements:
        key = (e.get("type"), e.get("id"))
        if key in seen:
            continue
        seen.add(key)
        tags = e.get("tags") or {}
        is_platform = tags.get("railway") == "platform" or (
            tags.get("public_transport") == "platform" and tags.get("train") == "yes")
        if is_platform:
            shapes = [line for line in _lines(e) if len(line) >= 2]
            if shapes:
                platforms.append({"refs": platform_refs(tags), "name": tags.get("name"), "shapes": shapes,
                                  "covered": tags.get("covered") == "yes" or tags.get("shelter") == "yes"})
        elif tags.get("railway") == "rail":
            for line in _lines(e):
                if len(line) >= 2:
                    tracks.append({"line": line, "siding": tags.get("service") in ("siding", "yard", "spur")})
    platforms.sort(key=lambda p: [int(r) if r.isdigit() else 999 for r in p["refs"]] or [999])
    return {"platforms": platforms, "tracks": tracks}


def platform_point(platform: dict) -> LatLon:
    """Middle of a platform, for placing a train marker on it."""
    points = [p for shape in platform["shapes"] for p in shape]
    return [sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)]


async def load_station_layout(client: httpx.AsyncClient, data_dir: Path, station: Station) -> dict:
    path = data_dir / "stations" / f"{station.crs}.json"
    if path.exists():
        cached = json.loads(path.read_text())
        if time.time() - cached.get("fetched", 0) < MAX_AGE:
            return cached["layout"]
    around = f"(around:{RADIUS_M},{station.lat},{station.lon})"
    query = (f'[out:json][timeout:60];(way["railway"="platform"]{around};relation["railway"="platform"]{around};'
             f'way["public_transport"="platform"]["train"="yes"]{around};way["railway"="rail"]{around};);out tags geom;')
    layout = parse_layout(await overpass(client, query, f"the layout of {station.name}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched": time.time(), "layout": layout}))
    return layout
