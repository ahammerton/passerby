"""Ships worth a second look, and which of them deserve an alert.

- Emergency: an emergency beacon (AIS-SART, man overboard, EPIRB), or a ship whose status
  says its distress beacon is active.
- Notable: a few famous ships by name; cruise ships (passenger ships 200 m or longer);
  warships 60 m or longer; and any ship 300 m or longer (the biggest container ships and tankers).
- Military: smaller military craft (patrol boats and the like). Listed, but never alerted:
  near a naval base they're constant.

Notable ships alert once a day each, and only while under way: a cruise ship sitting at its
berth all day isn't news, but one sailing past is.
"""

import time

# Names as ships broadcast them -> what they are.
NOTABLE_NAMES = {
    "QUEEN MARY 2": "Cunard's transatlantic ocean liner",
    "HMS QUEEN ELIZABETH": "Royal Navy aircraft carrier",
    "HMS PRINCE OF WALES": "Royal Navy aircraft carrier",
    "RRS SIR DAVID ATTENBOROUGH": "British polar research ship",
    "SIR DAVID ATTENBOROUGH": "British polar research ship",
}

CRUISE_MIN_M = 200
WARSHIP_MIN_M = 60
GIANT_MIN_M = 300
TYPE_WORDS = {"cargo": "container or cargo ship", "tanker": "tanker", "passenger": "passenger ship"}

ALERT_REPEAT_SECONDS = 24 * 3600
ALERT_LEVELS = {"all": {"emergency", "notable"}, "emergencies": {"emergency"}, "off": set()}


def assess(ship: dict) -> dict | None:
    """{"level", "reason"} if the ship is interesting, else None."""
    length = ship.get("length_m") or 0
    category = ship.get("category")
    if category == "beacon":
        return {"level": "emergency", "reason": ship.get("type") or "Emergency beacon"}
    if ship.get("status") == "Emergency beacon active":
        return {"level": "emergency", "reason": "Emergency beacon active"}
    if reason := NOTABLE_NAMES.get((ship.get("name") or "").upper()):
        return {"level": "notable", "reason": reason}
    if category == "passenger" and length >= CRUISE_MIN_M:
        return {"level": "notable", "reason": f"Cruise ship, {length:.0f} m"}
    if category == "military" and ship.get("type_code") == 35:
        if length >= WARSHIP_MIN_M:
            return {"level": "notable", "reason": f"Warship, {length:.0f} m"}
        return {"level": "military", "reason": "Military"}
    if length >= GIANT_MIN_M:
        return {"level": "notable", "reason": f"Giant {TYPE_WORDS.get(category, 'ship')}, {length:.0f} m"}
    return None


def under_way(ship: dict) -> bool:
    return not ship.get("still") and (ship.get("speed_kt") or 0) >= 2


class ShipInterest:
    def __init__(self, alerts: str = "all"):
        self.alert_levels = ALERT_LEVELS[alerts]
        self._alerted: dict[int, float] = {}

    def new_alerts(self, ships: list[dict], now: float | None = None) -> list[dict]:
        """Interesting ships to alert about now. An emergency beacon alerts as soon as it's seen;
        a notable ship when it's first seen under way, then at most once a day."""
        now = time.time() if now is None else now
        result = []
        for s in ships:
            level = (s.get("interest") or {}).get("level")
            if level not in self.alert_levels or (level == "notable" and not under_way(s)):
                continue
            last = self._alerted.get(s["mmsi"])
            if last is None or now - last >= ALERT_REPEAT_SECONDS:
                result.append(s)
                self._alerted[s["mmsi"]] = now
            elif level == "emergency":
                self._alerted[s["mmsi"]] = now  # still transmitting: no repeat while it lasts
        self._alerted = {k: t for k, t in self._alerted.items() if now - t < 2 * ALERT_REPEAT_SECONDS}
        return result
