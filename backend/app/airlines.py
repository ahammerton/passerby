"""Which airline is flying an aircraft, its colours for the map icon, and its logo.

The airline comes from the callsign: airline flights use the operator's 3-letter
ICAO code followed by the flight number (BAW291 is British Airways, EZY12AB easyJet).

The colours are hand-picked and stylised for a small top-down icon: `body` is the
fuselage crown and wings seen from above (white for most airlines), `tail` the
tail section, standing in for the fin you couldn't otherwise see from above.
Airlines not listed here still get their logo, just not airline colours.

Logos come from https://github.com/Jxck-S/airline-logos (FlightAware's square
logos, or RadarBox's for airlines FlightAware doesn't have), keyed by ICAO code,
and are cached on disk.
"""

import asyncio
import logging
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

WHITE = "#f8f8f8"

# ICAO code -> (name, body colour, tail colour)
AIRLINES: dict[str, tuple[str, str, str]] = {
    # UK and Ireland
    "BAW": ("British Airways", WHITE, "#c8102e"),
    "SHT": ("British Airways", WHITE, "#c8102e"),  # UK domestic "Shuttle" callsigns
    "CFE": ("BA CityFlyer", WHITE, "#c8102e"),
    "EFW": ("BA Euroflyer", WHITE, "#c8102e"),
    "VIR": ("Virgin Atlantic", "#ececf0", "#da0530"),
    "EZY": ("easyJet", WHITE, "#ff6600"),
    "EJU": ("easyJet Europe", WHITE, "#ff6600"),
    "EZS": ("easyJet Switzerland", WHITE, "#ff6600"),
    "RYR": ("Ryanair", WHITE, "#073590"),
    "RUK": ("Ryanair UK", WHITE, "#073590"),
    "EXS": ("Jet2", WHITE, "#e2001a"),
    "TOM": ("TUI Airways", WHITE, "#70cbf4"),
    "EIN": ("Aer Lingus", WHITE, "#006272"),
    "EUK": ("Aer Lingus UK", WHITE, "#006272"),
    "LOG": ("Loganair", WHITE, "#26306b"),
    # Europe
    "DLH": ("Lufthansa", WHITE, "#05164d"),
    "CLH": ("Lufthansa CityLine", WHITE, "#05164d"),
    "EWG": ("Eurowings", WHITE, "#a3074d"),
    "AFR": ("Air France", WHITE, "#002157"),
    "KLM": ("KLM", "#00a1de", "#00a1de"),
    "KLC": ("KLM Cityhopper", "#00a1de", "#00a1de"),
    "TRA": ("Transavia", WHITE, "#00d66c"),
    "TVF": ("Transavia France", WHITE, "#00d66c"),
    "IBE": ("Iberia", WHITE, "#d7192d"),
    "IBS": ("Iberia Express", WHITE, "#d7192d"),
    "VLG": ("Vueling", WHITE, "#ffcc00"),
    "SWR": ("Swiss", WHITE, "#e30613"),
    "AUA": ("Austrian", WHITE, "#ed1c24"),
    "SAS": ("SAS", WHITE, "#00256b"),
    "FIN": ("Finnair", WHITE, "#0b1560"),
    "ICE": ("Icelandair", WHITE, "#003e7e"),
    "NAX": ("Norwegian", WHITE, "#d81939"),
    "NOZ": ("Norwegian", WHITE, "#d81939"),
    "NSZ": ("Norwegian", WHITE, "#d81939"),
    "LOT": ("LOT Polish Airlines", WHITE, "#11397e"),
    "AEE": ("Aegean", WHITE, "#00539b"),
    "WZZ": ("Wizz Air", WHITE, "#c6007e"),
    "WUK": ("Wizz Air UK", WHITE, "#c6007e"),
    "WMT": ("Wizz Air Malta", WHITE, "#c6007e"),
    "BTI": ("airBaltic", WHITE, "#c4d600"),
    "THY": ("Turkish Airlines", WHITE, "#e81932"),
    # Middle East and Africa
    "UAE": ("Emirates", WHITE, "#d71921"),
    "QTR": ("Qatar Airways", "#e9e9eb", "#5c0632"),
    "ETD": ("Etihad", "#efe8da", "#bd8b13"),
    "ETH": ("Ethiopian", WHITE, "#009a44"),
    "MSR": ("EgyptAir", WHITE, "#10437f"),
    "RAM": ("Royal Air Maroc", WHITE, "#c1272d"),
    # Asia and Australia
    "AIC": ("Air India", WHITE, "#c8102e"),
    "SIA": ("Singapore Airlines", WHITE, "#1d2c5e"),
    "CPA": ("Cathay Pacific", WHITE, "#006564"),
    "QFA": ("Qantas", WHITE, "#e40000"),
    "JAL": ("Japan Airlines", WHITE, "#cc0000"),
    "ANA": ("ANA", WHITE, "#133d8c"),
    "KAL": ("Korean Air", "#a5d1ef", WHITE),
    "THA": ("Thai Airways", WHITE, "#5e2a84"),
    "MAS": ("Malaysia Airlines", WHITE, "#cc0000"),
    "CCA": ("Air China", WHITE, "#e60012"),
    "CSN": ("China Southern", WHITE, "#2f80ce"),
    # Americas
    "AAL": ("American Airlines", "#c4c8cc", "#0078d2"),
    "UAL": ("United Airlines", WHITE, "#002244"),
    "DAL": ("Delta", WHITE, "#003366"),
    "ACA": ("Air Canada", WHITE, "#000000"),
    "WJA": ("WestJet", WHITE, "#00aaa5"),
    "JBU": ("JetBlue", WHITE, "#003876"),
    # Cargo
    "FDX": ("FedEx", "#4d148c", "#4d148c"),
    "UPS": ("UPS", WHITE, "#351c15"),
    "DHK": ("DHL", "#ffcc00", "#ffcc00"),
    "BCS": ("DHL", "#ffcc00", "#ffcc00"),  # European Air Transport, flying for DHL
}

# Airline-style callsigns (same as the route lookup): three letters, then a digit.
AIRLINE_CALLSIGN = re.compile(r"^([A-Z]{3})[0-9][0-9A-Z]{0,3}$")
ICAO_CODE = re.compile(r"^[A-Z]{3}$")


def identify(callsign: str | None, military: bool = False, route_airline: str | None = None) -> dict:
    """Airline fields for one aircraft: {"airline_icao", "airline_name", "livery"}, or {} if none."""
    m = AIRLINE_CALLSIGN.match((callsign or "").strip().upper())
    # Military callsigns look the same (RRR = RAF) but aren't airlines.
    if not m or military:
        return {}
    code = m.group(1)
    known = AIRLINES.get(code)
    return {
        "airline_icao": code,
        "airline_name": known[0] if known else route_airline,
        "livery": {"body": known[1], "tail": known[2]} if known else None,
    }


# ---------- logos ----------

LOGO_SOURCES = (
    "https://raw.githubusercontent.com/Jxck-S/airline-logos/main/flightaware_logos/{}.png",
    "https://raw.githubusercontent.com/Jxck-S/airline-logos/main/radarbox_logos/{}.png",
)
LOGO_RETRY = 7 * 86400  # how long a "no logo" answer is kept
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class Logos:
    """Airline logos, fetched once and kept in data/logos/ (a `.none` file marks a miss)."""

    def __init__(self, client: httpx.AsyncClient, folder: Path):
        self.client = client
        self.folder = folder
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, code: str) -> Path | None:
        png, miss = self.folder / f"{code}.png", self.folder / f"{code}.none"
        if png.exists():
            return png
        if miss.exists() and time.time() - miss.stat().st_mtime < LOGO_RETRY:
            return None
        async with self._locks.setdefault(code, asyncio.Lock()):
            if png.exists():
                return png
            self.folder.mkdir(parents=True, exist_ok=True)
            for url in LOGO_SOURCES:
                resp = await self.client.get(url.format(code))
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()  # anything else: try again next time
                if resp.content.startswith(PNG_MAGIC):
                    png.write_bytes(resp.content)
                    miss.unlink(missing_ok=True)
                    return png
            miss.touch()
            return None
