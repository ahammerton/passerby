"""Rain radar frames from RainViewer (https://www.rainviewer.com/api.html), free, no key.

The browser may only fetch from the app itself (the content security policy), so the list
of frames is fetched here and handed on; the radar images are map tiles, loaded directly.
RainViewer's free tiles go up to zoom 7, so the map stretches them when zoomed in further.
"""

import re
import time

import httpx

INDEX_URL = "https://api.rainviewer.com/public/weather-maps.json"
HOST = "https://tilecache.rainviewer.com"
CACHE_S = 300
MAX_ZOOM = 7
# 256 px tiles, colour scheme 2 (universal blue), smoothed, with snow shown separately.
TILE = "/256/{z}/{x}/{y}/2/1_1.png"
_PATH = re.compile(r"^/v2/radar/[0-9a-f]{6,64}$")

_cache: dict = {"at": 0.0, "frames": None}


def parse(index: dict) -> list[dict]:
    """[{time, url}] for the past frames, oldest first; anything unexpected is dropped."""
    if index.get("host") != HOST:
        return []
    frames = []
    for f in (index.get("radar") or {}).get("past") or []:
        path, at = f.get("path"), f.get("time")
        if isinstance(path, str) and _PATH.match(path) and isinstance(at, int):
            frames.append({"time": at, "url": HOST + path + TILE})
    return sorted(frames, key=lambda f: f["time"])


async def frames(client: httpx.AsyncClient, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    if _cache["frames"] is None or now - _cache["at"] > CACHE_S:
        resp = await client.get(INDEX_URL, timeout=15)
        resp.raise_for_status()
        _cache.update(at=now, frames=parse(resp.json()))
    return _cache["frames"]
