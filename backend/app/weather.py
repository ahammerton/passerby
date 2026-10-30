"""METAR observations from the NOAA Aviation Weather Center.

Docs: https://aviationweather.gov/data/api/
"""

import httpx

from .geo import bounding_box, haversine_km

METAR_URL = "https://aviationweather.gov/api/data/metar"


async def fetch_metars(
    client: httpx.AsyncClient, lat: float, lon: float, radius_km: float, limit: int = 8
) -> list[dict]:
    lamin, lomin, lamax, lomax = bounding_box(lat, lon, radius_km)
    resp = await client.get(
        METAR_URL,
        params={"bbox": f"{lamin},{lomin},{lamax},{lomax}", "format": "json"},
    )
    resp.raise_for_status()
    # The API answers 204 No Content when no station is in range.
    reports = resp.json() if resp.content else []
    result = []
    for r in reports:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        result.append({
            "icao": r.get("icaoId"),
            "name": r.get("name"),
            "raw": r.get("rawOb"),
            "flight_category": r.get("fltCat"),
            "temp_c": r.get("temp"),
            "wind_dir": r.get("wdir"),
            "wind_kt": r.get("wspd"),
            "visibility": r.get("visib"),
            "observed": r.get("obsTime"),
            "distance_km": round(haversine_km(lat, lon, r["lat"], r["lon"]), 1),
        })
    result.sort(key=lambda r: r["distance_km"])
    return result[:limit]
