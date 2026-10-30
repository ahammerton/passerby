"""Aircraft worth a second look: emergencies, notable types, types rarely seen here, and military.

- Emergency: the transponder squawks 7500 (hijack), 7600 (radio failure) or 7700 (emergency).
- Notable: a hand-picked list of special types (outsize freighters, bombers, warbirds, airships...).
- Rare here: an airliner, turboprop or military type that the sighting history has seen on
  only a few airframes recently. Light aircraft, gliders and private jets are left out: every
  small variant (PA28 Cherokee Arrow, RV-4...) would count as rare, and they aren't.
- Military: everything the feeds flag as military. Listed, but never alerted: around a
  military airfield they're constant.
"""

import time

from .models import Aircraft

EMERGENCY_SQUAWKS = {
    "7500": "Hijack (squawk 7500)",
    "7600": "Radio failure (squawk 7600)",
    "7700": "Emergency (squawk 7700)",
}

# ICAO type designator -> what it is
NOTABLE_TYPES = {
    "A3ST": "Airbus Beluga", "A337": "Airbus BelugaXL", "BLCF": "Boeing 747 Dreamlifter",
    "A124": "Antonov An-124", "A225": "Antonov An-225", "AN22": "Antonov An-22", "AN12": "Antonov An-12",
    "C5M": "Lockheed C-5 Galaxy", "B52": "Boeing B-52 Stratofortress", "B1": "Rockwell B-1 Lancer",
    "B2": "Northrop B-2 Spirit", "U2": "Lockheed U-2", "E3TF": "Boeing E-3 Sentry (AWACS)",
    "E3CF": "Boeing E-3 Sentry (AWACS)", "E6": "Boeing E-6 Mercury", "R135": "Boeing RC-135",
    "E4": "Boeing E-4 Nightwatch", "VC25": "Boeing VC-25 (Air Force One)", "TU95": "Tupolev Tu-95",
    "SPIT": "Supermarine Spitfire", "HURI": "Hawker Hurricane", "LANC": "Avro Lancaster",
    "B17": "Boeing B-17 Flying Fortress", "DC3": "Douglas DC-3 / Dakota", "P51": "North American P-51 Mustang",
    "MOSQ": "de Havilland Mosquito", "SHIP": "Airship", "CONC": "Concorde", "DC6": "Douglas DC-6",
    "L188": "Lockheed Electra", "VULC": "Avro Vulcan",
}

RARE_SHAPES = {"widebody", "narrowbody", "turboprop", "military"}
RARE_MAX_AIRFRAMES = 3  # this many different airframes of a type (or fewer) in the window = rare
RARE_WINDOW_DAYS = 90
# Below this much history everything looks rare, so rarity stays off.
MIN_HISTORY_DAYS = 7
MIN_HISTORY_AIRCRAFT = 500

REFRESH_SECONDS = 3600
ALERT_REPEAT_SECONDS = 12 * 3600
ALERT_LEVELS = {"all": {"emergency", "notable", "rare"}, "emergencies": {"emergency"}, "off": set()}


def assess(a: Aircraft, type_counts: dict[str, int] | None) -> dict | None:
    """{"level", "reason"} if the aircraft is interesting, else None. `type_counts` is None
    while there isn't enough history to judge rarity."""
    if a.squawk in EMERGENCY_SQUAWKS:
        return {"level": "emergency", "reason": EMERGENCY_SQUAWKS[a.squawk]}
    code = (a.type_code or "").upper().strip()
    if code in NOTABLE_TYPES:
        return {"level": "notable", "reason": NOTABLE_TYPES[code]}
    if type_counts is not None and code and a.shape in RARE_SHAPES:
        seen = type_counts.get(code, 0)
        if seen <= RARE_MAX_AIRFRAMES:
            name = a.aircraft_name or code
            # The count includes this airframe once it has been logged.
            others = "no other" if seen <= 1 else f"only {seen}"
            return {"level": "rare", "reason": f"Rare here: {others} {name} seen in {RARE_WINDOW_DAYS} days"}
    if a.military:
        return {"level": "military", "reason": "Military"}
    return None


class Interest:
    """Marks interesting aircraft in each snapshot and decides which deserve an alert."""

    def __init__(self, alerts: str = "all"):
        self.alert_levels = ALERT_LEVELS[alerts]
        self.type_counts: dict[str, int] | None = None
        self.refreshed = 0.0
        self._alerted: dict[tuple[str, str], float] = {}

    def due(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) - self.refreshed >= REFRESH_SECONDS

    def set_history(self, counts: dict[str, int], aircraft: int, days: float, now: float | None = None) -> None:
        """Type -> airframes seen in the window, plus how much history there is in total."""
        enough = aircraft >= MIN_HISTORY_AIRCRAFT and days >= MIN_HISTORY_DAYS
        self.type_counts = counts if enough else None
        self.refreshed = time.time() if now is None else now

    def annotate(self, snapshot: list[Aircraft]) -> list[Aircraft]:
        return [a.model_copy(update={"interest": assess(a, self.type_counts)}) for a in snapshot]

    def new_alerts(self, snapshot: list[Aircraft], now: float | None = None) -> list[Aircraft]:
        """Interesting aircraft to alert about now. An emergency alerts as soon as it's seen (and
        again if the squawk changes); notable and rare types once per airframe every 12 hours."""
        now = time.time() if now is None else now
        result = []
        for a in snapshot:
            level = (a.interest or {}).get("level")
            if level not in self.alert_levels:
                continue
            key = (a.icao24, a.squawk if level == "emergency" else level)
            last = self._alerted.get(key)
            if last is None or now - last >= ALERT_REPEAT_SECONDS:
                result.append(a)
                self._alerted[key] = now
            elif level == "emergency":
                self._alerted[key] = now  # still squawking: no repeat alert while it lasts
        # Forget airframes not seen for a day.
        self._alerted = {k: t for k, t in self._alerted.items() if now - t < 86400}
        return result
