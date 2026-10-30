"""Merges aircraft from every source into one view relative to home."""

import time

from .geo import bearing_deg, compass, haversine_km
from .models import Aircraft


class Aggregator:
    def __init__(
        self,
        *,
        home_lat: float,
        home_lon: float,
        radius_km: float,
        alert_radius_km: float,
        alert_min_altitude_ft: float = 0.0,
        stale_after: float,
        priorities: dict[str, int],
    ):
        self.home_lat = home_lat
        self.home_lon = home_lon
        self.radius_km = radius_km
        self.alert_radius_km = alert_radius_km
        self.alert_min_altitude_ft = alert_min_altitude_ft
        self.stale_after = stale_after
        self.priorities = priorities
        self._by_source: dict[str, dict[str, Aircraft]] = {}
        self._overhead: set[str] = set()

    def update(self, source: str, aircraft: list[Aircraft]) -> None:
        self._by_source[source] = {a.icao24: a for a in aircraft}

    def _pick(self, candidates: list[Aircraft]) -> Aircraft:
        # Prefer a positioned report, then the higher-priority source, then the newest.
        return max(
            candidates,
            key=lambda a: (a.has_position, self.priorities.get(a.source, 0), a.last_seen),
        )

    def _is_overhead(self, a: Aircraft, distance: float) -> bool:
        if a.on_ground or distance > self.alert_radius_km:
            return False
        return a.altitude_ft is None or a.altitude_ft >= self.alert_min_altitude_ft

    def snapshot(self, now: float | None = None) -> list[Aircraft]:
        """Current aircraft within radius of home, nearest first."""
        now = time.time() if now is None else now
        merged: dict[str, list[Aircraft]] = {}
        for aircraft in self._by_source.values():
            for a in aircraft.values():
                if now - a.last_seen <= self.stale_after:
                    merged.setdefault(a.icao24, []).append(a)

        result = []
        for candidates in merged.values():
            a = self._pick(candidates)
            if not a.has_position:
                continue
            distance = haversine_km(self.home_lat, self.home_lon, a.lat, a.lon)
            if distance > self.radius_km:
                continue
            bearing = bearing_deg(self.home_lat, self.home_lon, a.lat, a.lon)
            # Fill gaps (e.g. registration) from lower-priority sources.
            extras = {
                field: getattr(other, field)
                for other in candidates
                for field in ("callsign", "registration", "type_code", "origin_country")
                if getattr(a, field) is None and getattr(other, field) is not None
            }
            result.append(a.model_copy(update={
                **extras,
                "distance_km": round(distance, 2),
                "bearing_deg": round(bearing, 1),
                "direction": compass(bearing),
                "overhead": self._is_overhead(a, distance),
                "military": any(c.military for c in candidates),
            }))
        result.sort(key=lambda a: a.distance_km)
        return result

    def new_overhead(self, snapshot: list[Aircraft]) -> list[Aircraft]:
        """Aircraft that entered the alert radius since the previous call."""
        current = {a.icao24: a for a in snapshot if a.overhead}
        entered = [a for icao, a in current.items() if icao not in self._overhead]
        self._overhead = set(current)
        return entered
