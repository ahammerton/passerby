"""Decoding AIS radio messages (NMEA !AIVDM sentences) from your own receiver.

A receiver such as AIS-catcher (with an RTL-SDR dongle) sends each message it hears as a
line like `!AIVDM,1,1,,B,15M67FC000G?ufbE`FepT@3n00Sa,0*5C`. The payload packs the message
into 6-bit characters; longer messages (ship details, type 5) come in two sentences.

Only the messages the app uses are decoded: positions (types 1-3, 18, 19) and ship details
(types 5 and 24), from ITU-R M.1371. Each becomes a message in aisstream.io's shape, so the
rest of the app treats both feeds alike.

Format reference: https://gpsd.gitlab.io/gpsd/AIVDM.html
"""

import time
from datetime import datetime, timezone

SIXBIT_TEXT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
PART_TIMEOUT = 10  # seconds to wait for the rest of a multi-sentence message


def checksum_ok(sentence: str) -> bool:
    body, star, given = sentence.partition("*")
    if not star:
        return True  # some receivers leave the checksum off
    calc = 0
    for ch in body[1:]:
        calc ^= ord(ch)
    return given[:2].upper() == f"{calc:02X}"


class Bits:
    """A message payload as a string of '0'/'1', read by bit position."""

    def __init__(self, payload: str, fill: int):
        bits = []
        for ch in payload:
            v = ord(ch) - 48
            if v > 40:
                v -= 8
            if not 0 <= v < 64:
                raise ValueError(f"bad payload character {ch!r}")
            bits.append(f"{v:06b}")
        self.bits = "".join(bits)
        if fill:
            self.bits = self.bits[:-fill]

    def __len__(self) -> int:
        return len(self.bits)

    def uint(self, start: int, length: int) -> int:
        return int(self.bits[start:start + length] or "0", 2)

    def int(self, start: int, length: int) -> int:
        value = self.uint(start, length)
        return value - (1 << length) if value >> (length - 1) else value

    def text(self, start: int, length: int) -> str:
        chunk = self.bits[start:start + length]
        return "".join(SIXBIT_TEXT[int(chunk[i:i + 6], 2)] for i in range(0, len(chunk) - 5, 6))


def _time_utc(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000000000 +0000 UTC")


def _dimension(b: Bits, start: int) -> dict:
    return {"A": b.uint(start, 9), "B": b.uint(start + 9, 9), "C": b.uint(start + 18, 6), "D": b.uint(start + 24, 6)}


def _position(b: Bits, lon_at: int, sog_at: int, cog_at: int, heading_at: int) -> dict:
    return {
        "Sog": b.uint(sog_at, 10) / 10,  # 102.3: not available
        "Longitude": b.int(lon_at, 28) / 600000,  # 181: not available
        "Latitude": b.int(lon_at + 28, 27) / 600000,  # 91: not available
        "Cog": b.uint(cog_at, 12) / 10,  # 360: not available
        "TrueHeading": b.uint(heading_at, 9),  # 511: not available
    }


def decode_payload(b: Bits) -> tuple[str, int, dict] | None:
    """(aisstream message type, MMSI, message body) for the message types the app uses."""
    kind, mmsi = b.uint(0, 6), b.uint(8, 30)
    if kind in (1, 2, 3) and len(b) >= 137:
        return "PositionReport", mmsi, {**_position(b, 61, 50, 116, 128), "NavigationalStatus": b.uint(38, 4)}
    if kind == 18 and len(b) >= 133:
        return "StandardClassBPositionReport", mmsi, _position(b, 57, 46, 112, 124)
    if kind == 19 and len(b) >= 301:
        return "ExtendedClassBPositionReport", mmsi, {
            **_position(b, 57, 46, 112, 124), "Name": b.text(143, 120), "Type": b.uint(263, 8),
            "Dimension": _dimension(b, 271)}
    if kind == 5 and len(b) >= 422:
        return "ShipStaticData", mmsi, {
            "ImoNumber": b.uint(40, 30), "CallSign": b.text(70, 42), "Name": b.text(112, 120), "Type": b.uint(232, 8),
            "Dimension": _dimension(b, 240),
            "Eta": {"Month": b.uint(274, 4), "Day": b.uint(278, 5), "Hour": b.uint(283, 5), "Minute": b.uint(288, 6)},
            "MaximumStaticDraught": b.uint(294, 8) / 10, "Destination": b.text(302, 120)}
    if kind == 24 and len(b) >= 160:
        part_b = b.uint(38, 2) == 1
        if not part_b:
            return "StaticDataReport", mmsi, {"PartNumber": False, "ReportA": {"Valid": True, "Name": b.text(40, 120)}}
        return "StaticDataReport", mmsi, {"PartNumber": True, "ReportB": {
            "Valid": True, "ShipType": b.uint(40, 8), "CallSign": b.text(90, 42), "Dimension": _dimension(b, 132)}}
    return None


class NmeaDecoder:
    """Feed it lines; it returns aisstream-shaped messages once each is complete."""

    def __init__(self):
        self._parts: dict[tuple[str, str], tuple[float, int, list[str]]] = {}
        self.bad = 0

    def feed(self, line: str, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        line = line.strip()
        if line.startswith("\\"):  # an NMEA 4.10 tag block before the sentence: \s:...*hh\!AIVDM...
            line = line[line.find("\\", 1) + 1:]
        if not (line.startswith("!") and line[3:6] in ("VDM", "VDO")):
            return None
        if not checksum_ok(line):
            self.bad += 1
            return None
        fields = line.split("*")[0].split(",")
        if len(fields) < 7:
            self.bad += 1
            return None
        try:
            count, number, fill = int(fields[1]), int(fields[2]), int(fields[6] or 0)
        except ValueError:
            self.bad += 1
            return None
        seq, channel, payload = fields[3], fields[4], fields[5]
        if count > 1:
            key = (seq, channel)
            started, expected, parts = self._parts.get(key, (now, count, []))
            if number == 1:
                started, expected, parts = now, count, []
            if number != len(parts) + 1 or now - started > PART_TIMEOUT:
                self._parts.pop(key, None)
                return None
            parts.append(payload)
            if len(parts) < expected:
                self._parts[key] = (started, expected, parts)
                return None
            self._parts.pop(key, None)
            payload = "".join(parts)
            if len(self._parts) > 100:  # forget halves whose other half never came
                self._parts = {k: v for k, v in self._parts.items() if now - v[0] <= PART_TIMEOUT}
        try:
            decoded = decode_payload(Bits(payload, fill))
        except ValueError:
            self.bad += 1
            return None
        if not decoded:
            return None
        kind, mmsi, body = decoded
        meta = {"MMSI": mmsi, "ShipName": "", "time_utc": _time_utc(now)}
        if "Latitude" in body:
            meta |= {"latitude": body["Latitude"], "longitude": body["Longitude"]}
        return {"MessageType": kind, "MetaData": meta, "Message": {kind: {"UserID": mmsi, "Valid": True, **body}}}
