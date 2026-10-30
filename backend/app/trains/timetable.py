"""Network Rail SCHEDULE (timetable) feed, filtered to trains running near home.

The full national timetable (~140 MB gzipped JSON lines, regenerated nightly)
is downloaded once, then kept current with the small daily update files.
Every file carries a sequence number; updates are applied strictly in order,
and any gap triggers a fresh full download. Only schedules that call at or
pass a station in the area, and run in the next couple of weeks, are kept.

Format: https://wiki.openraildata.com/index.php?title=SCHEDULE
"""

import asyncio
import gzip
import json
import logging
import re
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

UK = ZoneInfo("Europe/London")
FEED_URL = "https://publicdatafeeds.networkrail.co.uk/ntrod/CifFileAuthenticate"
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Keep schedules running within this many days; a full download refreshes the window weekly.
HORIZON_DAYS = 14
FULL_EVERY_DAYS = 7
# Update files are published around 01:30 UK time for the previous day.
PUBLISHED_AFTER = (2, 0)
# When two schedules for one train cover the same date: cancellation, overlay, new, permanent.
STP_PRECEDENCE = {"C": 0, "O": 1, "N": 2, "P": 3}

OPERATORS = {
    "AW": "Transport for Wales", "CC": "c2c", "CH": "Chiltern Railways", "CS": "Caledonian Sleeper",
    "EM": "East Midlands Railway", "ES": "Eurostar", "GC": "Grand Central", "GN": "Great Northern",
    "GR": "LNER", "GW": "Great Western Railway", "GX": "Gatwick Express", "HT": "Hull Trains",
    "HX": "Heathrow Express", "IL": "Island Line", "LD": "Lumo", "LE": "Greater Anglia",
    "LM": "West Midlands Railway", "LO": "London Overground", "ME": "Merseyrail", "NT": "Northern",
    "SE": "Southeastern", "SN": "Southern", "SR": "ScotRail", "SW": "South Western Railway",
    "TL": "Thameslink", "TP": "TransPennine Express", "VT": "Avanti West Coast", "XC": "CrossCountry",
    "XR": "Elizabeth line",
}
POWER_TYPES = {
    "EMU": "Electric unit", "DMU": "Diesel unit", "DEM": "Diesel-electric unit", "D": "Diesel locomotive",
    "E": "Electric locomotive", "ED": "Electro-diesel locomotive", "EML": "Electric unit with locomotive",
    "HST": "High Speed Train",
}
# Diesel multiple-unit timing loads are letter codes standing for classes. T, X and V were
# checked against the fleets actually running them locally (GWR Reading–Gatwick, SWR
# Waterloo–Exeter, CrossCountry); the rest are the standard CIF meanings.
DMU_TIMING_LOADS = {
    "A": "14x–17x", "E": "158/168/170/175", "N": "165/0", "S": "150/153/155/156",
    "T": "165/166", "V": "220/221", "X": "159",
}

CATERING = {
    "C": "Buffet", "F": "Restaurant (first class)", "H": "Hot food", "M": "Meal (first class)",
    "R": "Restaurant", "T": "Trolley",
}


def _public(value: str | None) -> int | None:
    """Advertised times use "0000" for "not advertised"."""
    return _hhmm(value) if value and value != "0000" else None


def _hhmm(value: str | None) -> int | None:
    """CIF time "1612" or "1612H" (half minute) → seconds after midnight."""
    if not value or not value[:4].isdigit():
        return None
    return int(value[:2]) * 3600 + int(value[2:4]) * 60 + (30 if value.endswith("H") else 0)


@dataclass
class Call:
    tiploc: str
    arrival: int | None  # working times, seconds after midnight of the run date
    departure: int | None
    passing: int | None
    public_arrival: int | None
    public_departure: int | None
    platform: str | None

    @property
    def public(self) -> bool:
        return self.public_arrival is not None or self.public_departure is not None

    @property
    def time(self) -> int | None:
        return self.departure if self.departure is not None else (
            self.passing if self.passing is not None else self.arrival)


@dataclass
class Schedule:
    uid: str
    stp: str
    start: date
    end: date
    days: str  # "1111100" = Monday–Friday
    atoc: str | None
    headcode: str | None
    power: str | None
    timing_load: str | None
    speed: str | None
    train_class: str | None
    catering: str | None
    sleepers: str | None
    category: str | None = None
    calls: list[Call] = field(default_factory=list)

    @property
    def bus(self) -> bool:
        return self.category in ("BR", "BS")  # replacement bus / scheduled bus

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.uid, self.start.isoformat(), self.stp)

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end and self.days[d.weekday()] == "1"

    def details(self) -> dict:
        """What the timetable says about the train, for the browser."""
        # For multiple units the timing load is the class: a number ("450") or, for diesel units, a
        # letter code. For locomotives it's the trailing load in tonnes instead.
        load = (self.timing_load or "").strip().upper()
        planned_class = trailing_load = None
        if self.power in ("EMU", "DMU", "DEM", "EML"):
            planned_class = load if load.isdigit() else DMU_TIMING_LOADS.get(load) if self.power != "EMU" else None
        elif self.power in ("D", "E", "ED") and load.isdigit():
            trailing_load = int(load)
        passenger = self.atoc not in (None, "ZZ")  # "ZZ" is freight and other non-passenger operators
        return {
            "operator": OPERATORS.get(self.atoc or "", self.atoc if passenger else None),
            "power": POWER_TYPES.get(self.power or ""),
            "planned_class": planned_class,
            "trailing_load_t": trailing_load,
            "max_speed_mph": int(self.speed) if (self.speed or "").isdigit() else None,
            # Train class "S" is standard only; "B" or blank means first and standard.
            "first_class": passenger and self.train_class in (None, "", "B"),
            "catering": CATERING.get((self.catering or "")[:1]),
            "sleeper": bool(self.sleepers),
        }


def to_row(s: Schedule) -> list:
    return [s.uid, s.stp, s.start.isoformat(), s.end.isoformat(), s.days, s.atoc, s.headcode, s.power,
            s.timing_load, s.speed, s.train_class, s.catering, s.sleepers, s.category,
            [[c.tiploc, c.arrival, c.departure, c.passing, c.public_arrival, c.public_departure, c.platform]
             for c in s.calls]]


def from_row(row: list) -> Schedule:
    *head, calls = row
    uid, stp, start, end, days, *rest = head
    return Schedule(uid, stp, date.fromisoformat(start), date.fromisoformat(end), days, *rest,
                    calls=[Call(*c) for c in calls])


def parse_schedule(record: dict) -> Schedule:
    seg = record.get("schedule_segment") or {}
    calls = []
    for loc in seg.get("schedule_location") or []:
        calls.append(Call(
            tiploc=loc["tiploc_code"].strip(),
            arrival=_hhmm(loc.get("arrival")),
            departure=_hhmm(loc.get("departure")),
            passing=_hhmm(loc.get("pass")),
            public_arrival=_public(loc.get("public_arrival")),
            public_departure=_public(loc.get("public_departure")),
            platform=(loc.get("platform") or "").strip() or None,
        ))
    # Times after midnight belong to the next day: make them keep increasing.
    offset, last = 0, -1
    for c in calls:
        t = c.time if c.time is not None else c.arrival
        if t is not None:
            if t + offset < last - 6 * 3600:
                offset += 86400
            last = t + offset
        for attr in ("arrival", "departure", "passing", "public_arrival", "public_departure"):
            v = getattr(c, attr)
            if v is not None:
                setattr(c, attr, v + offset)
    return Schedule(
        uid=record["CIF_train_uid"].strip(),
        stp=record.get("CIF_stp_indicator") or "P",
        start=date.fromisoformat(record["schedule_start_date"]),
        end=date.fromisoformat(record.get("schedule_end_date") or record["schedule_start_date"]),
        days=record.get("schedule_days_runs") or "1111111",
        atoc=record.get("atoc_code"),
        headcode=seg.get("signalling_id"),
        power=seg.get("CIF_power_type"),
        timing_load=seg.get("CIF_timing_load"),
        speed=seg.get("CIF_speed"),
        train_class=seg.get("CIF_train_class"),
        catering=seg.get("CIF_catering_code"),
        sleepers=seg.get("CIF_sleepers"),
        category=seg.get("CIF_train_category"),
        calls=calls,
    )


class Timetable:
    def __init__(self, area_tiplocs: set[str]):
        self.area_tiplocs = area_tiplocs
        self.schedules: dict[tuple[str, str, str], Schedule] = {}
        self.by_uid: dict[str, set[tuple[str, str, str]]] = {}
        self.by_headcode: dict[str, set[str]] = {}
        self.sequence: int | None = None
        self.generated: float | None = None  # when the last applied file was produced
        self.full_built: float | None = None
        self._pattern = re.compile(r'"tiploc_code":\s*"(' + "|".join(map(re.escape, sorted(area_tiplocs))) + r')"') \
            if area_tiplocs else None

    # ---------- building ----------

    def _add(self, s: Schedule) -> None:
        self.schedules[s.key] = s
        self.by_uid.setdefault(s.uid, set()).add(s.key)
        if s.headcode:
            self.by_headcode.setdefault(s.headcode, set()).add(s.uid)

    def _remove(self, key: tuple[str, str, str]) -> None:
        if self.schedules.pop(key, None) is not None:
            self.by_uid[key[0]].discard(key)

    def _in_window(self, record: dict, today: date) -> bool:
        start = date.fromisoformat(record["schedule_start_date"])
        end = date.fromisoformat(record.get("schedule_end_date") or record["schedule_start_date"])
        return start <= today + timedelta(days=HORIZON_DAYS) and end >= today - timedelta(days=1)

    def apply(self, lines: Iterable[str], today: date) -> None:
        """Apply a full or update file (JSON lines), keeping only schedules for the area."""
        cancellations: list[Schedule] = []
        for line in lines:
            if not line.startswith('{"JsonScheduleV1"'):
                if line.startswith('{"JsonTimetableV1"'):
                    header = json.loads(line)["JsonTimetableV1"]
                    self.sequence = header["Metadata"]["sequence"]
                    self.generated = header["timestamp"]
                continue
            # Cheap text check first: most of the 700k national schedules never come near home.
            in_area = bool(self._pattern and self._pattern.search(line))
            is_delete = '"transaction_type":"Delete"' in line
            is_cancel = '"CIF_stp_indicator":"C"' in line
            if not (in_area or is_delete or is_cancel):
                continue
            record = json.loads(line)["JsonScheduleV1"]
            if record.get("transaction_type") == "Delete":
                key = (record["CIF_train_uid"].strip(), record["schedule_start_date"], record.get("CIF_stp_indicator") or "P")
                self._remove(key)
                continue
            if not self._in_window(record, today):
                continue
            s = parse_schedule(record)
            if s.stp == "C":
                # Cancellations carry no stops, so they're kept if the train itself is one of ours.
                cancellations.append(s)
            else:
                self._add(s)
        for s in cancellations:
            if s.uid in self.by_uid and self.by_uid[s.uid]:
                self._add(s)

    def prune(self, today: date) -> None:
        for key, s in list(self.schedules.items()):
            if s.end < today - timedelta(days=1):
                self._remove(key)

    # ---------- queries ----------

    def schedule_for(self, uid: str, run_date: date) -> tuple[Schedule | None, bool]:
        """(schedule in force for a train on a date, whether it's planned cancelled)."""
        candidates = [self.schedules[k] for k in self.by_uid.get(uid, ()) if self.schedules[k].covers(run_date)]
        if not candidates:
            return None, False
        best = min(candidates, key=lambda s: STP_PRECEDENCE.get(s.stp, 9))
        if best.stp == "C":
            # The cancelled train's stops come from the schedule it cancels.
            running = [s for s in candidates if s.stp != "C"]
            base = min(running, key=lambda s: STP_PRECEDENCE.get(s.stp, 9)) if running else None
            return base, True
        return best, False

    def match(self, headcode: str, tiplocs: set[str], when: float, late_minutes: int | None,
              tolerance: float = 30 * 60) -> tuple[Schedule, date] | None:
        """Find a train from a live report: its headcode, where it was and when.

        Used when TRUST didn't send an activation (e.g. the train was already
        running when the app started). The schedule must have this headcode,
        call at or pass one of the reporting TIPLOCs, and be due there within
        `tolerance` of the report once its lateness is allowed for.
        """
        best, best_gap = None, tolerance
        report_day = datetime.fromtimestamp(when, UK).date()
        for run_date in (report_day, report_day - timedelta(days=1)):  # overnight trains started yesterday
            for uid in self.by_headcode.get(headcode, ()):
                s, cancelled = self.schedule_for(uid, run_date)
                if s is None or cancelled:
                    continue
                for c in s.calls:
                    if c.tiploc in tiplocs and c.time is not None:
                        due = run_seconds_to_ts(run_date, c.time) + (late_minutes or 0) * 60
                        gap = abs(due - when)
                        if gap < best_gap:
                            best, best_gap = (s, run_date), gap
        return best

    def running_on(self, run_date: date) -> Iterable[tuple[Schedule, bool]]:
        """Every (schedule, cancelled) in force on a date, one per train."""
        for uid in list(self.by_uid):
            s, cancelled = self.schedule_for(uid, run_date)
            if s is not None:
                yield s, cancelled

    # ---------- persistence ----------

    def save(self, path: Path) -> None:
        data = {"sequence": self.sequence, "generated": self.generated, "full_built": self.full_built,
                "area": sorted(self.area_tiplocs), "schedules": [to_row(s) for s in self.schedules.values()]}
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode()))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path, area_tiplocs: set[str]) -> "Timetable | None":
        if not path.exists():
            return None
        data = json.loads(gzip.decompress(path.read_bytes()))
        if set(data.get("area", [])) != area_tiplocs:
            return None  # home or radius changed: rebuild for the new area
        tt = cls(area_tiplocs)
        tt.sequence, tt.generated, tt.full_built = data["sequence"], data["generated"], data["full_built"]
        for row in data["schedules"]:
            tt._add(from_row(row))
        return tt


def run_seconds_to_ts(run_date: date, seconds: int) -> float:
    """Seconds after midnight of a UK run date → unix time (DST-aware)."""
    midnight = datetime(run_date.year, run_date.month, run_date.day, tzinfo=UK)
    return (midnight + timedelta(seconds=seconds)).timestamp()


def next_update_name(generated: float) -> str:
    """The update after a file produced on day X is named after day X, published early on X+1."""
    return DAY_NAMES[datetime.fromtimestamp(generated, UK).weekday()]


def update_due(generated: float, now: float) -> bool:
    produced = datetime.fromtimestamp(generated, UK).date()
    publish = datetime(produced.year, produced.month, produced.day, *PUBLISHED_AFTER, tzinfo=UK) + timedelta(days=1)
    return now >= publish.timestamp()


class TimetableFeed:
    """Downloads and maintains the area timetable."""

    def __init__(self, client: httpx.AsyncClient, username: str, password: str, data_dir: Path, area_tiplocs: set[str]):
        self.client = client
        self.auth = (username, password)
        self.path = data_dir / "timetable.json.gz"
        self.area_tiplocs = area_tiplocs
        self.timetable: Timetable | None = None

    async def _download(self, file_type: str, day: str, dest: Path) -> None:
        async with self.client.stream("GET", FEED_URL, params={"type": file_type, "day": day}, auth=self.auth,
                                      follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(1 << 20):
                    f.write(chunk)

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.readlines()

    async def _full(self, today: date) -> Timetable:
        log.info("Downloading the full Network Rail timetable (~140 MB)")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "full.gz"
            await self._download("CIF_ALL_FULL_DAILY", "toc-full", dest)
            tt = Timetable(self.area_tiplocs)

            def build():
                with gzip.open(dest, "rt", encoding="utf-8") as f:
                    tt.apply(f, today)

            await asyncio.to_thread(build)
        tt.full_built = time.time()
        log.info("Timetable: %d schedules for the area (sequence %s)", len(tt.schedules), tt.sequence)
        return tt

    async def _apply_updates(self, tt: Timetable, today: date) -> bool:
        """Apply any published updates in order. False if there's a gap (needs a full download)."""
        while tt.generated and update_due(tt.generated, time.time()):
            name = next_update_name(tt.generated)
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "update.gz"
                await self._download("CIF_ALL_UPDATE_DAILY", f"toc-update-{name}", dest)
                lines = await asyncio.to_thread(self._read_lines, dest)
            header = json.loads(lines[0])["JsonTimetableV1"]
            seq = header["Metadata"]["sequence"]
            if seq <= tt.sequence:
                return True  # tonight's file isn't out yet; the old one is still there
            if seq != tt.sequence + 1:
                log.warning("Timetable update gap (have %s, found %s): fetching the full timetable", tt.sequence, seq)
                return False
            await asyncio.to_thread(tt.apply, lines, today)
            log.info("Applied timetable update %s (%s)", seq, name)
        return True

    async def refresh(self) -> Timetable:
        today = datetime.fromtimestamp(time.time(), UK).date()
        tt = self.timetable or await asyncio.to_thread(Timetable.load, self.path, self.area_tiplocs)
        stale = tt is None or not tt.full_built or time.time() - tt.full_built > FULL_EVERY_DAYS * 86400
        if stale or not await self._apply_updates(tt, today):
            tt = await self._full(today)
            await self._apply_updates(tt, today)
        tt.prune(today)
        await asyncio.to_thread(tt.save, self.path)
        self.timetable = tt
        return tt
