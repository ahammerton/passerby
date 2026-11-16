"""Turning aisstream.io's AIS messages into plain ship facts.

Every message has a `MetaData` header (the ship's MMSI number, name, position and time) and
the decoded AIS message under `Message[MessageType]`. Positions and details come in separate
messages: large ships (class A) send `PositionReport` and `ShipStaticData`; small boats
(class B) send `StandardClassBPositionReport` and `StaticDataReport` (in two parts).

AIS marks unknown values with out-of-range numbers (latitude 91, speed 102.3, heading 511...),
which become None here.
"""

import time
from datetime import datetime, timezone

from .flags import MID_COUNTRY

POSITION_TYPES = {"PositionReport": "A", "StandardClassBPositionReport": "B", "ExtendedClassBPositionReport": "B"}
DETAIL_TYPES = {"ShipStaticData", "StaticDataReport", "ExtendedClassBPositionReport"}
MESSAGE_TYPES = sorted(set(POSITION_TYPES) | DETAIL_TYPES)

NAV_STATUS = {
    0: "Under way", 1: "At anchor", 2: "Not under command", 3: "Restricted manoeuvrability",
    4: "Constrained by draught", 5: "Moored", 6: "Aground", 7: "Fishing", 8: "Under way sailing",
    14: "Emergency beacon active",
}
STILL_STATUS = {1, 5, 6}  # at anchor, moored, aground

# Ship type code -> (category for the icon, description)
SPECIFIC_TYPES = {
    30: ("fishing", "Fishing vessel"), 31: ("tug", "Towing"), 32: ("tug", "Towing (large tow)"),
    33: ("special", "Dredger"), 34: ("special", "Diving vessel"), 35: ("military", "Military"),
    36: ("sailing", "Sailing boat"), 37: ("pleasure", "Pleasure craft"), 50: ("tug", "Pilot boat"),
    51: ("sar", "Search and rescue"), 52: ("tug", "Tug"), 53: ("tug", "Port tender"),
    54: ("special", "Anti-pollution vessel"), 55: ("military", "Law enforcement"),
    58: ("special", "Medical transport"), 59: ("special", "Non-combatant ship"),
}
TYPE_DECADES = {
    2: ("other", "Wing-in-ground craft"), 4: ("highspeed", "High-speed craft"), 6: ("passenger", "Passenger ship"),
    7: ("cargo", "Cargo ship"), 8: ("tanker", "Tanker"), 9: ("other", "Other vessel"),
}
LEISURE = {"sailing", "pleasure"}

# No real ship is longer (the largest are about 400 m); bigger figures are typing errors.
MAX_LENGTH_M = 460
MAX_BEAM_M = 80


def text(value) -> str | None:
    """AIS text is padded with spaces or '@'."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.replace("@", " ").split())
    return cleaned or None


def ship_type(code) -> tuple[str, str | None]:
    """(category, description) for an AIS ship type code; ("unknown", None) when not given."""
    if not isinstance(code, int) or code <= 0:
        return "unknown", None
    if code in SPECIFIC_TYPES:
        return SPECIFIC_TYPES[code]
    return TYPE_DECADES.get(code // 10, ("other", None))


def flag(mmsi: int) -> str | None:
    """ISO country code of the flag, from the first three digits of the MMSI (its MID)."""
    digits = str(mmsi)
    if len(digits) != 9:
        return None
    if digits[:2] == "98":  # a craft carried by a ship: its tender or lifeboat
        digits = digits[2:]
    return MID_COUNTRY.get(int(digits[:3]))


def is_vessel(mmsi: int) -> bool:
    """Ships and their craft, and emergency beacons; not shore stations or buoys."""
    digits = str(mmsi)
    return len(digits) == 9 and (digits[0] in "234567" or digits[:2] == "98" or digits[:3] in BEACONS)


# MMSI prefixes of emergency beacons: AIS-SART, man overboard, EPIRB.
BEACONS = {"970": "AIS-SART (survival craft)", "972": "Man overboard beacon", "974": "EPIRB distress beacon"}


def beacon(mmsi: int) -> str | None:
    return BEACONS.get(str(mmsi)[:3]) if len(str(mmsi)) == 9 else None


def dimensions(dim) -> dict:
    if not isinstance(dim, dict):
        return {}
    length, beam = (dim.get("A") or 0) + (dim.get("B") or 0), (dim.get("C") or 0) + (dim.get("D") or 0)
    return {"length_m": length if 0 < length <= MAX_LENGTH_M else None,
            "beam_m": beam if 0 < beam <= MAX_BEAM_M else None}


def eta(value, now: float) -> float | None:
    """AIS ETAs have no year (and 0/24/60 for "not given"). Pick the year that puts it within
    about a month in the past to eleven months ahead."""
    if not isinstance(value, dict):
        return None
    month, day, hour, minute = (value.get(k) for k in ("Month", "Day", "Hour", "Minute"))
    if not (month and day) or not 1 <= month <= 12 or hour is None or hour > 23 or minute is None or minute > 59:
        return None
    this_year = datetime.fromtimestamp(now, timezone.utc).year
    for year in (this_year, this_year + 1, this_year - 1):
        try:
            t = datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
        if now - 35 * 86400 <= t <= now + 335 * 86400:
            return t
    return None


def reported_at(meta: dict, now: float) -> float:
    """MetaData time_utc looks like '2026-11-16 16:08:02.663927083 +0000 UTC'."""
    try:
        t = datetime.strptime(str(meta.get("time_utc", ""))[:19], "%Y-%m-%d %H:%M:%S")
        t = t.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return now
    return min(t, now)  # never in the future


def _number(value, invalid: float) -> float | None:
    """A speed or course, or None for AIS's "not available" value (or anything above it)."""
    return value if isinstance(value, (int, float)) and 0 <= value < invalid else None


def parse(msg: dict, now: float | None = None) -> tuple[int, dict, dict] | None:
    """(mmsi, position update, details update) from one aisstream message, or None to ignore it."""
    now = time.time() if now is None else now
    kind = msg.get("MessageType")
    meta = msg.get("MetaData") or {}
    body = (msg.get("Message") or {}).get(kind)
    mmsi = meta.get("MMSI") or (body or {}).get("UserID")
    if not isinstance(body, dict) or not isinstance(mmsi, int) or not is_vessel(mmsi):
        return None
    position, details = {}, {}
    name = text(meta.get("ShipName"))
    if name:
        details["name"] = name
    if kind in POSITION_TYPES:
        lat, lon = body.get("Latitude"), body.get("Longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and abs(lat) <= 90 and abs(lon) <= 180 \
                and (lat, lon) != (0, 0):
            heading = body.get("TrueHeading")
            status = body.get("NavigationalStatus")
            position = {
                "lat": lat, "lon": lon,
                "speed_kt": _number(body.get("Sog"), 102.3),
                "course": _number(body.get("Cog"), 360),
                "heading": heading if isinstance(heading, int) and 0 <= heading < 360 else None,
                "status": NAV_STATUS.get(status) if kind == "PositionReport" else None,
                "still": status in STILL_STATUS if kind == "PositionReport" else False,
                "ais_class": POSITION_TYPES[kind],
                "reported": reported_at(meta, now),
            }
    if kind == "ShipStaticData":
        imo = body.get("ImoNumber")
        draught = body.get("MaximumStaticDraught")
        details |= {
            "name": text(body.get("Name")) or name,
            "callsign": text(body.get("CallSign")),
            "imo": imo if isinstance(imo, int) and 1000000 <= imo <= 9999999 else None,
            "type_code": body.get("Type"),
            "destination": text(body.get("Destination")),
            "eta": eta(body.get("Eta"), now),
            "draught_m": draught if isinstance(draught, (int, float)) and 0 < draught < 25.5 else None,
            **dimensions(body.get("Dimension")),
        }
    elif kind == "StaticDataReport":
        # Part A carries the name, part B the type, call sign and size.
        if body.get("PartNumber") and (b := body.get("ReportB") or {}).get("Valid"):
            details |= {"callsign": text(b.get("CallSign")), "type_code": b.get("ShipType"),
                        **dimensions(b.get("Dimension"))}
        elif not body.get("PartNumber") and (a := body.get("ReportA") or {}).get("Valid"):
            details["name"] = text(a.get("Name")) or name
    elif kind == "ExtendedClassBPositionReport":
        details |= {"name": text(body.get("Name")) or name, "type_code": body.get("Type"),
                    **dimensions(body.get("Dimension"))}
    # A detail that isn't given never overwrites one that was.
    details = {k: v for k, v in details.items() if v is not None and not (k == "type_code" and not v)}
    if not position and not details:
        return None
    return mmsi, position, details
