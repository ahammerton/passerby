"""Network Rail CORPUS reference data: STANOX / TIPLOC location codes → names and CRS codes.

Downloaded with the NROD login and cached in the data directory for a week.
"""

import gzip
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

CORPUS_URL = "https://publicdatafeeds.networkrail.co.uk/ntrod/SupportingFileAuthenticate?type=CORPUS"
MAX_AGE = 7 * 86400


@dataclass(frozen=True)
class Location:
    stanox: str
    name: str
    crs: str | None


def parse_corpus(data: dict) -> dict[str, Location]:
    locations: dict[str, Location] = {}
    for row in data.get("TIPLOCDATA", []):
        stanox = (row.get("STANOX") or "").strip()
        if not stanox:
            continue
        crs = (row.get("3ALPHA") or "").strip() or None
        name = (row.get("NLCDESC") or "").strip().title() or stanox
        # Several TIPLOCs can share a STANOX; prefer the one with a CRS code (the station).
        if stanox not in locations or (crs and not locations[stanox].crs):
            locations[stanox] = Location(stanox, name, crs)
    return locations


@dataclass(frozen=True)
class Tiploc:
    tiploc: str
    stanox: str | None
    name: str
    crs: str | None


def parse_tiplocs(data: dict) -> dict[str, Tiploc]:
    """TIPLOCs (the codes timetables use) with their STANOX (the codes TRUST reports use)."""
    tiplocs = {}
    for row in data.get("TIPLOCDATA", []):
        code = (row.get("TIPLOC") or "").strip()
        if code:
            tiplocs[code] = Tiploc(
                code,
                (row.get("STANOX") or "").strip() or None,
                (row.get("NLCDESC") or "").strip().title() or code,
                (row.get("3ALPHA") or "").strip() or None,
            )
    return tiplocs


def read_tiplocs(data_dir: Path) -> dict[str, Tiploc]:
    """TIPLOC index from the cached CORPUS file (load_corpus must have run first)."""
    return parse_tiplocs(json.loads(gzip.decompress((data_dir / "corpus.json.gz").read_bytes())))


async def load_corpus(client: httpx.AsyncClient, data_dir: Path, username: str, password: str) -> dict[str, Location]:
    path = data_dir / "corpus.json.gz"
    stale = not path.exists() or time.time() - path.stat().st_mtime > MAX_AGE
    if stale:
        try:
            log.info("Downloading Network Rail CORPUS reference data")
            # Redirects to a signed S3 URL; httpx drops the auth header on the cross-host hop.
            resp = await client.get(CORPUS_URL, auth=(username, password), follow_redirects=True, timeout=60)
            resp.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
        except httpx.HTTPError as exc:
            if not path.exists():
                raise
            log.warning("CORPUS refresh failed, using cached copy: %s", exc)
    return parse_corpus(json.loads(gzip.decompress(path.read_bytes())))
