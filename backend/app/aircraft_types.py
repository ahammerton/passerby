"""What kind of aircraft something is, for picking its map icon.

Mostly from the ICAO type designator (B77W, A320, C172, EC35, GLF6, ...), which
the community ADS-B feeds and adsbdb provide. Without one, the ADS-B emitter
category the transponder broadcasts (A1 light ... A7 rotorcraft) is used.
Military aircraft keep their shape (a Chinook is still a helicopter) and are
flagged so the map can colour them; fast jets get the military jet shape.
"""

from .models import Aircraft

# shape -> label shown to the user
SHAPES = {
    "widebody": "Large commercial jet",
    "narrowbody": "Commercial jet",
    "turboprop": "Turboprop airliner",
    "bizjet": "Private jet",
    "light": "Light aircraft",
    "helicopter": "Helicopter",
    "military": "Military jet",
    "glider": "Glider",
    "balloon": "Balloon",
    "drone": "Drone",
    "vehicle": "Ground vehicle",
    "unknown": "Aircraft",
}

# ICAO type designators, matched exactly or by prefix (longest match wins).
_TYPES: dict[str, tuple[str, ...]] = {
    "widebody": ("A30B", "A306", "A310", "A33", "A34", "A35", "A38", "B74", "B76", "B77", "B78", "MD11",
                 "DC10", "L101", "IL96", "A124", "A225", "A3ST", "BLCF", "B52",
                 # Large military transports, tankers and surveillance jets.
                 "A400", "C5M", "K35", "KC10", "KC46", "E3", "R135", "IL76", "A50", "VC10"),
    "narrowbody": ("A318", "A319", "A32", "A20N", "A21N", "A22", "BCS1", "BCS3", "B71", "B72", "B73", "B37M",
                   "B38M", "B39M", "B3XM", "B75", "E17", "E19", "E29", "E75", "CRJ", "MD8", "MD9", "DC9", "B46",
                   "RJ70", "RJ85", "RJ1H", "F70", "F100", "C919", "SU95", "T204", "P8", "E737", "C40"),
    "turboprop": ("AT4", "AT7", "DH8", "SF34", "SB20", "JS31", "JS32", "JS41", "D328", "E120", "F50", "SW4",
                  "B190", "DHC6", "L410", "AN24", "AN26", "D228", "S340",
                  "C130", "C30J", "CN35", "C295", "C27J", "AN12", "AN32"),
    "bizjet": ("C25", "C50", "C51", "C52", "C55", "C56", "C65", "C68", "C70", "C75", "CL30", "CL35", "CL60",
               "GL5T", "GL6T", "GL7T", "GLEX", "GLF", "G150", "G200", "G280", "E35L", "E50P", "E55P", "E545",
               "E550", "F2TH", "F900", "FA7X", "FA8X", "FA50", "FA10", "FA20", "LJ", "H25", "PC24", "PRM1",
               "HDJT", "BE40", "ASTR", "GALX", "SF50", "EA50", "C680", "C700",
               # Gulfstream model names as some databases give them.
               "G450", "G550", "G650", "G700", "G800", "GA5C", "GA6C", "GA7C", "GA8C"),
    "helicopter": ("EC", "AS3", "AS5", "AS6", "A109", "A119", "A139", "A149", "A169", "A189", "AW", "B06",
                   "B105", "B212", "B412", "B429", "B407", "B427", "B505", "R22", "R44", "R66", "S76", "S92",
                   "S61", "EH10", "H47", "CH47", "LYNX", "PUMA", "WILD", "H160", "H175", "NH90", "UH1", "H60",
                   "MI8", "GAZL", "SA3", "SA34", "SA36", "SA65", "EXPL", "MD52", "MD60", "H500", "EN28", "EN48",
                   "CABR", "G2CA", "HUCO", "OH58", "TIGR", "BK17", "B47G", "AH64", "H64", "MRLX", "A129"),
    "military": ("EUFI", "F35", "F15", "F16", "F18", "FA18", "F22", "TOR", "HAWK", "T6", "TUCA", "PC21", "A10",
                 "RFAL", "M2K", "GRIF", "JAGR", "F5", "T38", "L39", "AJET", "PC9", "TEX2", "SR71", "U2", "B1",
                 "B2", "F117", "MIR2", "SU27", "MG29", "F4", "F14", "T45", "M346", "T50"),
    # Common light aircraft. Longer prefixes elsewhere still win (C25 private jet, C130 turboprop).
    "light": ("C1", "C2", "C3", "C4", "C77R", "P28", "PA", "P32", "P46", "SR2", "DA", "BE3", "BE5", "BE9", "BE2",
              "B350", "M20", "AA5", "TB", "DR4", "RV", "C42", "EV97", "PC12", "PC6", "TBM", "KODI", "DHC2", "J3",
              "CUB", "ULAC", "G115", "BN2", "TRIS", "SIRA", "A210", "BT36", "AT3", "GA8", "P180"),
    "glider": ("GLID", "ASK", "ASW", "ASG", "ASH2", "DG", "LS4", "LS6", "LS8", "DUOD", "ARCP", "VENT",
               "NIMB", "JANU", "DISC", "PIK", "SZD"),
    "balloon": ("BALL",),
    "drone": ("UAV", "Q9", "Q4", "HRON", "WK45"),
}
# Codes that would wrongly prefix-match others: "P3" is the Orion but "P32R" a Piper;
# "C17" is the Globemaster but "C172" a Cessna.
_EXACT = {"P3": "turboprop", "C17": "widebody"}
# Longest prefixes first, so e.g. "F50" (turboprop) beats "F5" (military) and "A109" (helicopter) beats "A10".
_PREFIXES = sorted(((p, shape) for shape, ps in _TYPES.items() for p in ps), key=lambda x: -len(x[0]))

# ADS-B emitter categories (DO-260B).
_CATEGORIES = {
    "A1": "light", "A2": "turboprop", "A3": "narrowbody", "A4": "narrowbody", "A5": "widebody",
    "A6": "military", "A7": "helicopter", "B1": "glider", "B2": "balloon", "B4": "light", "B6": "drone",
    "C1": "vehicle", "C2": "vehicle", "C3": "vehicle",
}
# Words in adsbdb's model names, for aircraft with neither a known type nor a category.
_NAME_WORDS = (("helicopter", "helicopter"), ("copter", "helicopter"), ("cessna 1", "light"),
               ("piper", "light"), ("cirrus", "light"), ("diamond", "light"), ("gulfstream", "bizjet"),
               ("learjet", "bizjet"), ("citation", "bizjet"), ("falcon", "bizjet"), ("challenger", "bizjet"),
               ("global", "bizjet"), ("atr", "turboprop"), ("dash 8", "turboprop"), ("dhc-8", "turboprop"))


def known_type(type_code: str | None) -> str | None:
    """Shape for a type designator in the tables, or None."""
    code = (type_code or "").upper().strip()
    if not code:
        return None
    if code in _EXACT:
        return _EXACT[code]
    for prefix, shape in _PREFIXES:
        if code.startswith(prefix):
            return shape
    return None


def shape_for_type(type_code: str | None) -> str | None:
    if not (type_code or "").strip():
        return None
    # Anything else with a type code is almost always a light aircraft (C172, PA28, SR22, DA42, ...).
    return known_type(type_code) or "light"


def _from_name(name: str | None) -> str | None:
    name = (name or "").lower()
    return next((s for word, s in _NAME_WORDS if word in name), None) if name else None


def classify(a: Aircraft) -> dict:
    """{"shape", "kind"} for one aircraft."""
    # A known type code first; then the broadcast category; then the model name; and only then
    # the "unrecognised type code = light aircraft" guess (a "G450" is a Gulfstream, not a Cessna).
    shape = (known_type(a.type_code) or _CATEGORIES.get(a.category or "") or _from_name(a.aircraft_name)
             or shape_for_type(a.type_code) or "unknown")
    if not a.military:
        return {"shape": shape, "kind": SHAPES[shape]}
    # Military aircraft keep their real shape (tankers and transports look like airliners);
    # the map colours them differently.
    if shape == "unknown":
        shape = "military"
    return {"shape": shape, "kind": _MILITARY_KINDS.get(shape, "Military aircraft")}


_MILITARY_KINDS = {
    "widebody": "Military transport", "narrowbody": "Military transport", "turboprop": "Military transport",
    "bizjet": "Military jet", "military": "Military jet", "helicopter": "Military helicopter",
    "light": "Military light aircraft", "drone": "Military drone",
}
