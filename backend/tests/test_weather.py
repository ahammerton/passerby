import asyncio

import httpx

from app.weather import decode, fetch_metars, describe_weather, station_name, visibility_km

FARNBOROUGH = {
    "icaoId": "EGLF", "name": "Farnborough Arpt, EN, GB", "lat": 51.28, "lon": -0.773,
    "rawOb": "METAR EGLF 161520Z AUTO 10001KT 9999 BKN040/// 06/02 Q1004", "fltCat": "VFR",
    "temp": 6, "dewp": 2, "wdir": 100, "wspd": 1, "visib": "6+", "altim": 1004, "obsTime": 1794842400,
    "cover": "BKN", "clouds": [{"cover": "BKN", "base": 4000}],
}


def test_decoded_report():
    w = decode(FARNBOROUGH, 51.28, -0.773)
    assert w["name"] == "Farnborough Airport" and w["icao"] == "EGLF"
    assert (w["visibility_km"], w["visibility_plus"]) == (10.0, True)
    assert w["clouds"] == [{"cover": "Broken cloud", "base_ft": 4000}] and w["sky"] is None
    assert w["weather"] == [] and w["pressure_hpa"] == 1004 and w["distance_km"] == 0
    assert w["raw"] == FARNBOROUGH["rawOb"]


def test_clear_skies():
    clear = {**FARNBOROUGH, "rawOb": "METAR EGLL 161520Z 25006KT CAVOK 07/01 Q1005", "clouds": [], "cover": "CAVOK"}
    w = decode(clear, 51.28, -0.773)
    assert w["sky"] == "No significant cloud" and w["visibility_km"] == 10.0
    assert decode({**clear, "rawOb": "EGLC 161520Z 24006KT 9999 NCD 08/M01 Q1005", "cover": "CLR"},
                  51.28, -0.773)["sky"] == "No cloud"


def test_visibility():
    assert visibility_km("METAR EGKK 161520Z 22010KT 0800 FG", None) == (0.8, False)
    assert visibility_km("METAR EGKK 161520Z 22010KT 4500NDV -RA Q1005", None) == (4.5, False)
    # US reports in statute miles: use the API's figure. The pressure isn't a visibility.
    assert visibility_km("METAR KJFK 161551Z 31012KT 10SM FEW250 A3002 RMK AO2 SLP165", "10+") == (16.1, True)
    assert visibility_km("METAR KBOS 161551Z 31012KT 1 1/2SM BR", "1.5") == (2.4, False)
    assert visibility_km(None, None) == (None, False)


def test_present_weather_in_words():
    assert describe_weather("-SHRA BR") == ["light showers of rain", "mist"]
    assert describe_weather("+TSRA") == ["heavy thunderstorm with rain"]
    assert describe_weather("VCSH TS FZFG") == ["showers nearby", "thunderstorm", "freezing fog"]
    assert describe_weather("RASN") == ["rain and snow"]
    assert describe_weather("NSW XYZQ") == ["NSW", "XYZQ"]  # unknown codes pass through
    assert describe_weather(None) == []


def test_station_names():
    assert station_name("London/Heathrow Intl, EN, GB") == "Heathrow International"
    assert station_name(None) is None


def test_fetch_sends_a_short_bounding_box():
    seen = []

    def handler(request):
        seen.append(request.url.params["bbox"])
        return httpx.Response(200, json=[FARNBOROUGH])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_metars(client, 51.47, -0.4543, 100)
    assert [w["icao"] for w in asyncio.run(scenario())] == ["EGLF"]
    assert all(len(n.split(".")[1]) == 4 for n in seen[0].split(","))
