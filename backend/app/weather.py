"""METAR observations from the NOAA Aviation Weather Center, decoded for people.

Docs: https://aviationweather.gov/data/api/

A METAR is pilot shorthand ("24012KT 9999 FEW030 12/07 Q1018"). The API already
splits out most of it; this module fills the gaps (visibility in metres, present
weather in words) and tidies the station name. The raw report is kept too.
"""

import re

import httpx

from .geo import bounding_box, haversine_km

METAR_URL = "https://aviationweather.gov/api/data/metar"

CLOUD_COVER = {"FEW": "Few clouds", "SCT": "Scattered cloud", "BKN": "Broken cloud",
               "OVC": "Overcast", "VV": "Sky obscured"}
# Sky codes that mean no cloud worth reporting.
CLEAR_SKY = {"CLR": "No cloud", "SKC": "No cloud", "NCD": "No cloud",
             "NSC": "No significant cloud", "CAVOK": "No significant cloud"}

WX_INTENSITY = {"-": "light", "+": "heavy"}
WX_DESCRIPTOR = {"MI": "shallow", "BC": "patches of", "PR": "partial", "DR": "drifting",
                 "BL": "blowing", "SH": "showers of", "TS": "thunderstorm with", "FZ": "freezing"}
WX_PHENOMENON = {
    "DZ": "drizzle", "RA": "rain", "SN": "snow", "SG": "snow grains", "IC": "ice crystals",
    "PL": "ice pellets", "GR": "hail", "GS": "small hail", "UP": "unknown precipitation",
    "BR": "mist", "FG": "fog", "FU": "smoke", "VA": "volcanic ash", "DU": "dust", "SA": "sand",
    "HZ": "haze", "PY": "spray", "PO": "dust whirls", "SQ": "squalls", "FC": "funnel cloud",
    "SS": "sandstorm", "DS": "duststorm",
}
WX_CODE = re.compile(r"^(VC)?([-+])?((?:MI|BC|PR|DR|BL|SH|TS|FZ)?)((?:[A-Z]{2})*)$")

STATUTE_MILE_KM = 1.609344


def describe_weather(wx: str | None) -> list[str]:
    """'-SHRA BR' -> ['light showers of rain', 'mist']. Unknown codes are left as they are."""
    result = []
    for code in (wx or "").split():
        m = WX_CODE.match(code)
        parts = [m.group(4)[i:i + 2] for i in range(0, len(m.group(4)), 2)] if m else []
        if not m or any(p not in WX_PHENOMENON for p in parts) or not (parts or m.group(3)):
            result.append(code)
            continue
        vicinity, intensity, descriptor = m.groups()[:3]
        words = [WX_INTENSITY.get(intensity or "", ""), WX_DESCRIPTOR.get(descriptor, "")]
        if descriptor == "TS" and not parts:
            words[1] = "thunderstorm"
        words.append(" and ".join(WX_PHENOMENON[p] for p in parts))
        text = " ".join(w for w in words if w)
        if descriptor == "SH" and not parts:
            text = text.replace("showers of", "showers")
        result.append(f"{text} nearby" if vicinity else text)
    return result


def visibility_km(raw: str | None, visib) -> tuple[float | None, bool]:
    """(km, "or more") from the raw report's metres (most of the world), falling back
    to the API's statute miles ("6+" means 6 or more)."""
    tokens = (raw or "").split()
    if "RMK" in tokens:
        tokens = tokens[:tokens.index("RMK")]
    for t in tokens[2:]:  # past "METAR"/station and the time
        if t == "CAVOK":
            return 10.0, True
        if re.fullmatch(r"\d{4}(NDV)?", t):
            metres = int(t[:4])
            return (10.0, True) if metres == 9999 else (metres / 1000, False)
    if visib is None:
        return None, False
    text = str(visib).strip()
    plus = text.endswith("+")
    try:
        miles = eval_fraction(text.rstrip("+"))
    except ValueError:
        return None, False
    return round(miles * STATUTE_MILE_KM, 1), plus


def eval_fraction(text: str) -> float:
    """'10', '1/2' or '1 1/2' miles."""
    total = 0.0
    for part in text.split():
        if "/" in part:
            num, den = part.split("/")
            total += int(num) / int(den)
        else:
            total += float(part)
    return total


def describe_sky(raw: str | None, clouds: list | None, cover: str | None) -> tuple[list[dict], str | None]:
    """Cloud layers as [{"cover": "Broken cloud", "base_ft": 4000}], or a clear-sky phrase."""
    layers = [
        {"cover": CLOUD_COVER.get(c.get("cover"), c.get("cover")), "base_ft": c.get("base")}
        for c in clouds or [] if c.get("cover") in CLOUD_COVER
    ]
    if layers:
        return layers, None
    if "CAVOK" in (raw or "").split():
        return [], CLEAR_SKY["CAVOK"]
    return [], CLEAR_SKY.get(cover or "")


def station_name(name: str | None) -> str | None:
    """'Farnborough Arpt, EN, GB' -> 'Farnborough Airport'; 'London/Gatwick Airport' -> 'Gatwick Airport'."""
    if not name:
        return None
    place = name.split(",")[0].split("/")[-1].strip()
    return re.sub(r"\bArpt\b", "Airport", re.sub(r"\bIntl\b", "International", place))


def decode(r: dict, lat: float, lon: float) -> dict:
    vis_km, vis_plus = visibility_km(r.get("rawOb"), r.get("visib"))
    layers, sky = describe_sky(r.get("rawOb"), r.get("clouds"), r.get("cover"))
    wdir = r.get("wdir")
    return {
        "icao": r.get("icaoId"),
        "name": station_name(r.get("name")),
        "raw": r.get("rawOb"),
        "flight_category": r.get("fltCat"),
        "temp_c": r.get("temp"),
        "dewpoint_c": r.get("dewp"),
        # Degrees the wind blows from, or "VRB" for variable.
        "wind_dir": wdir if isinstance(wdir, (int, float)) or wdir == "VRB" else None,
        "wind_kt": r.get("wspd"),
        "gust_kt": r.get("wgst"),
        "visibility": r.get("visib"),
        "visibility_km": vis_km,
        "visibility_plus": vis_plus,
        "clouds": layers,
        "sky": sky,
        "weather": describe_weather(r.get("wxString")),
        "pressure_hpa": r.get("altim"),
        "observed": r.get("obsTime"),
        "distance_km": round(haversine_km(lat, lon, r["lat"], r["lon"]), 1),
    }


async def fetch_metars(
    client: httpx.AsyncClient, lat: float, lon: float, radius_km: float, limit: int = 8
) -> list[dict]:
    lamin, lomin, lamax, lomax = bounding_box(lat, lon, radius_km)
    resp = await client.get(
        METAR_URL,
        # The API rejects long decimals (400 "Must specify ... bounding box"); 4 places is ~10 m.
        params={"bbox": f"{lamin:.4f},{lomin:.4f},{lamax:.4f},{lomax:.4f}", "format": "json"},
    )
    resp.raise_for_status()
    # The API answers 204 No Content when no station is in range.
    reports = resp.json() if resp.content else []
    result = [decode(r, lat, lon) for r in reports if r.get("lat") is not None and r.get("lon") is not None]
    result.sort(key=lambda r: r["distance_km"])
    return result[:limit]
