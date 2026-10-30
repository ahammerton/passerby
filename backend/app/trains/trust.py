"""Parsing Network Rail TRUST train movement messages.

Messages arrive in batches (JSON arrays) on /topic/TRAIN_MVT_ALL_TOC.
Schema: https://github.com/openraildata/network-rail-json-schema
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")

ACTIVATION, CANCELLATION, MOVEMENT = "0001", "0002", "0003"

# First character of the headcode describes the kind of train.
TRAIN_CLASSES = {
    "0": "Light locomotive",
    "1": "Express passenger",
    "2": "Stopping passenger",
    "3": "Priority empty stock",
    "4": "Freight (75 mph)",
    "5": "Empty passenger stock",
    "6": "Freight (60 mph)",
    "7": "Freight (45 mph)",
    "8": "Freight (35 mph)",
    "9": "Passenger (class 9)",
}


def uk_local_ms(value: str | int | None) -> float | None:
    """TRUST event times are UK local wall-clock times encoded as epoch ms.

    Read the number as if it were UTC, then reinterpret those wall-clock
    fields in Europe/London (so BST times come out an hour earlier).
    """
    if value in (None, ""):
        return None
    naive = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).replace(tzinfo=None)
    return naive.replace(tzinfo=UK).timestamp()


def headcode(train_id: str) -> str:
    """Characters 3–6 of the 10-character TRUST train ID are the headcode, e.g. 2G41."""
    return train_id[2:6]


def train_class(code: str) -> str | None:
    return TRAIN_CLASSES.get(code[:1])


@dataclass
class Movement:
    train_id: str
    stanox: str
    event: str  # "ARRIVAL" or "DEPARTURE"
    time: float  # actual event time, unix seconds
    next_stanox: str | None
    next_run_seconds: float | None
    late_minutes: int | None
    terminated: bool
    toc_id: str | None
    platform: str | None

    @property
    def next_eta(self) -> float | None:
        if self.next_run_seconds is None:
            return None
        return self.time + self.next_run_seconds


def parse_activation(body: dict[str, Any]) -> tuple[str, str, date] | None:
    """(train ID, schedule UID, run date) from a train activation message."""
    train_id, uid = body.get("train_id"), (body.get("train_uid") or "").strip()
    if not train_id or not uid:
        return None
    origin = uk_local_ms(body.get("origin_dep_timestamp"))
    if origin is None:
        return None
    return train_id, uid, datetime.fromtimestamp(origin, UK).date()


def parse_movement(body: dict[str, Any]) -> Movement | None:
    time = uk_local_ms(body.get("actual_timestamp"))
    stanox = (body.get("loc_stanox") or "").strip()
    if time is None or not stanox or not body.get("train_id"):
        return None
    run_time = body.get("next_report_run_time")
    try:
        variation = int(body.get("timetable_variation") or 0)
    except ValueError:
        variation = 0
    status = body.get("variation_status")
    late = {"LATE": variation, "EARLY": -variation, "ON TIME": 0}.get(status)
    return Movement(
        train_id=body["train_id"],
        stanox=stanox,
        event=body.get("event_type") or "",
        time=time,
        next_stanox=(body.get("next_report_stanox") or "").strip() or None,
        next_run_seconds=float(run_time) * 60 if run_time not in (None, "") else None,
        late_minutes=late,
        terminated=body.get("train_terminated") == "true",
        toc_id=body.get("toc_id"),
        platform=(body.get("platform") or "").strip() or None,
    )
