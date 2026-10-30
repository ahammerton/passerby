"""Small geodesy helpers. Distances are in kilometres, angles in degrees."""

import math

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, 0–360 clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def bounding_box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(lat_min, lon_min, lat_max, lon_max) of a box enclosing the circle."""
    dlat = math.degrees(radius_km / EARTH_RADIUS_KM)
    # Guard against the poles, where longitude spread is unbounded.
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = min(math.degrees(radius_km / (EARTH_RADIUS_KM * cos_lat)), 180.0)
    return (
        max(lat - dlat, -90.0),
        max(lon - dlon, -180.0),
        min(lat + dlat, 90.0),
        min(lon + dlon, 180.0),
    )


COMPASS_POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass(bearing: float) -> str:
    return COMPASS_POINTS[round(bearing / 45) % 8]
