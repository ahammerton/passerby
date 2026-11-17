"""Rail disruptions near home, from National Rail's Disruptions API (Rail Data Marketplace).

https://raildata.org.uk: a free account and a subscription to "NationalRail Disruptions API"
(Rail Delivery Group) give a key, sent in the x-apikey header. Three parts are used:

- incidents, per station: what's disrupting trains there (planned engineering works and
  unplanned problems), with National Rail's advice and a link to its page;
- station messages, per station: the short notice shown at the station;
- service indicators: each train company's status ("Good service", or what's wrong).

All three are fetched every 5 minutes for the stations you watch (your watched line's two
stations and your departure boards).
"""

import asyncio
import html
import logging
import re
import time
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

URL = "https://api1.raildata.org.uk/1010-disruptions-experience-api-11_1"
INTERVAL_S = 300
MAX_STATIONS = 6
ALERT_REPEAT_S = 12 * 3600


class DisruptionsError(Exception):
    pass


def text(value: str | None) -> str | None:
    """National Rail's HTML as plain text: paragraphs and list items on their own lines."""
    if not value:
        return None
    value = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", value)
    value = html.unescape(re.sub(r"<[^>]+>", "", value))
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line) or None


def first_link(value: str | None) -> str | None:
    m = re.search(r'href="(https://[^"]+)"', value or "")
    return html.unescape(m[1]) if m else None


def _time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_incidents(payload: list) -> dict[str, dict]:
    """Incident id -> incident, with the stations it was listed for."""
    incidents: dict[str, dict] = {}
    for station in payload or []:
        crs = station.get("crsCode")
        for d in station.get("disruptions") or []:
            if not d.get("id"):
                continue
            item = incidents.get(d["id"])
            if item is None:
                links = d.get("disruptionLinks") or []
                item = incidents[d["id"]] = {
                    "id": d["id"],
                    "summary": text(d.get("summary")) or "Disruption",
                    "description": text(d.get("description")),
                    "routes": text(" ".join(r.get("routeDetails") or "" for r in d.get("affectedRoutes") or [])),
                    "planned": bool(d.get("isPlanned")),
                    "alert": bool(d.get("isAlert")),
                    "status": d.get("status"),
                    "operators": [{"code": o.get("tocCode"), "name": o.get("tocName")}
                                  for o in d.get("affectedOperators") or [] if o.get("tocCode")],
                    "start": _time(d.get("startDateTime")),
                    "end": _time(d.get("endDateTime")),
                    "updated": _time(d.get("lastModifiedDateTime")),
                    "url": next((link.get("uri") for link in links if link.get("uri")), None),
                    "stations": [],
                }
            if crs and crs not in item["stations"]:
                item["stations"].append(crs)
    return incidents


def parse_messages(payload: list) -> dict[str, dict]:
    """crs -> {messages: [{category, severity, text, url}], notice}."""
    out = {}
    for station in payload or []:
        crs = station.get("crsCode")
        if not crs:
            continue
        out[crs] = {
            "messages": [{"category": m.get("category"), "severity": m.get("severity"),
                          "text": text(m.get("summary")), "url": first_link(m.get("summary"))}
                         for m in station.get("stationDisruptions") or [] if m.get("summary")],
            "notice": text(station.get("stationAlerts")),
        }
    return out


def parse_indicators(payload: list) -> dict[str, dict]:
    """Train company code -> {name, good, status}."""
    out = {}
    for toc in payload or []:
        code = toc.get("tocCode")
        if not code:
            continue
        good = toc.get("tocStatus") == "Good service"
        out[code] = {"code": code, "name": toc.get("tocName") or code, "good": good,
                     "status": "Good service" if good else (toc.get("tocStatusDescription") or "").strip() or "Disruption"}
    return out


async def fetch(client: httpx.AsyncClient, api_key: str, path: str, params: dict | None = None):
    try:
        resp = await client.get(f"{URL}/{path}", params=params, timeout=30,
                                headers={"x-apikey": api_key, "Accept": "application/json"})
    except httpx.TimeoutException:
        raise DisruptionsError("National Rail didn't answer in time") from None
    except httpx.HTTPError as exc:
        raise DisruptionsError(f"couldn't reach National Rail ({type(exc).__name__})") from None
    if resp.status_code in (401, 403):
        raise DisruptionsError("the Rail Data Marketplace rejected the API key")
    if resp.status_code == 429:
        raise DisruptionsError("Rail Data Marketplace rate limit reached")
    if resp.status_code != 200:
        raise DisruptionsError(f"National Rail answered {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise DisruptionsError("National Rail sent data that isn't JSON") from None


async def check_key(client: httpx.AsyncClient, api_key: str) -> str | None:
    """None if the key works, else why not (one small request: the train companies' status)."""
    try:
        await fetch(client, api_key, "tocs/serviceIndicators")
    except DisruptionsError as exc:
        return f"{str(exc)[:1].upper()}{str(exc)[1:]}." if "rejected" in str(exc) \
            else f"Couldn't check the Rail Data Marketplace key: {exc}."
    return None


class RailDisruptions:
    def __init__(self, client: httpx.AsyncClient, api_key: str, stations: list[tuple[str, str]]):
        """`stations`: (code, name) of the stations to watch, nearest or most important first."""
        self.client, self.api_key = client, api_key
        self.stations = stations[:MAX_STATIONS]
        self.incidents: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.indicators: dict[str, dict] = {}
        self.operators: set[str] = set()  # train companies serving these stations, when the timetable says
        self.status: dict = {"ok": None, "last_update": None, "error": None}
        self.pending_alerts: list[dict] = []  # taken by the broadcast loop
        self._alerted: dict[str, float] = {}
        self._first = True

    async def update(self) -> None:
        incidents, messages = {}, {}
        for code, _ in self.stations:  # the API takes one station a request
            for item in parse_incidents(await fetch(self.client, self.api_key, "stations/disruptions/incidents",
                                                    {"crsCode": code})).values():
                known = incidents.setdefault(item["id"], item)
                known["stations"] = list(dict.fromkeys(known["stations"] + item["stations"]))
            messages |= parse_messages(await fetch(self.client, self.api_key, "stations/disruptions/stationMessages",
                                                   {"crsCode": code}))
        self.indicators = parse_indicators(await fetch(self.client, self.api_key, "tocs/serviceIndicators"))
        self.incidents, self.messages = incidents, messages
        self.status.update(ok=True, last_update=time.time(), error=None)
        self.pending_alerts += self.new_alerts()

    def relevant_operators(self) -> list[str]:
        """Train companies to show a status line for: those the timetable says call at these
        stations, else those named in their incidents."""
        named = {o["code"] for i in self.incidents.values() for o in i["operators"]}
        return sorted(self.operators or named)

    def snapshot(self) -> dict:
        names = dict(self.stations)
        incidents = sorted(self.incidents.values(), key=lambda i: (i["planned"], -(i["updated"] or 0)))
        operators = [self.indicators[c] for c in self.relevant_operators() if c in self.indicators]
        return {
            "status": self.status,
            "stations": [{"crs": c, "name": n, **self.messages.get(c, {"messages": [], "notice": None})}
                         for c, n in self.stations],
            "incidents": [i | {"station_names": [names.get(s, s) for s in i["stations"]]} for i in incidents],
            "operators": operators,
        }

    def new_alerts(self, now: float | None = None) -> list[dict]:
        """Unplanned disruptions not alerted before. Nothing on the first fetch after starting, so a
        restart doesn't repeat what's already known."""
        now = time.time() if now is None else now
        fresh = [i for i in self.incidents.values() if not i["planned"] and i["id"] not in self._alerted]
        for i in fresh:
            self._alerted[i["id"]] = now
        self._alerted = {k: t for k, t in self._alerted.items() if now - t < ALERT_REPEAT_S * 4}
        if self._first:
            self._first = False
            return []
        return fresh


    def take_alerts(self) -> list[dict]:
        alerts, self.pending_alerts = self.pending_alerts, []
        return alerts


async def run(disruptions: RailDisruptions) -> None:
    while True:
        try:
            await disruptions.update()
            delay = INTERVAL_S
        except DisruptionsError as exc:
            log.warning("Rail disruptions: %s", exc)
            disruptions.status.update(ok=False, error=str(exc))
            delay = INTERVAL_S * (3 if "rejected" in str(exc) else 1)
        except Exception as exc:
            log.warning("Rail disruptions: unexpected %s", type(exc).__name__)
            disruptions.status.update(ok=False, error=f"unexpected {type(exc).__name__}")
            delay = INTERVAL_S
        await asyncio.sleep(delay)
