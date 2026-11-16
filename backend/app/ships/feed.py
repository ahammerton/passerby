"""The live connection to aisstream.io.

The subscription (API key, area, message types) must be sent within 3 seconds of
connecting. Messages arrive whenever ships transmit; reading has to keep up, or the
service drops messages and may close the connection. Compression is on (aisstream.io
limits the bandwidth of uncompressed connections). A rejected key just closes the
connection, with no message, so "closed before confirming" means a bad key.

Docs: https://aisstream.io/documentation
"""

import asyncio
import json
import logging
import random
from collections.abc import Callable

import websockets

from ..geo import bounding_box
from .ais import MESSAGE_TYPES

log = logging.getLogger(__name__)

URL = "wss://stream.aisstream.io/v0/stream"
CONFIRM_TIMEOUT = 15
MAX_MESSAGE_BYTES = 1 << 20


def subscription(api_key: str, lat: float, lon: float, radius_km: float) -> dict:
    lat_min, lon_min, lat_max, lon_max = bounding_box(lat, lon, radius_km)
    return {
        "APIKey": api_key,
        # Corners as [latitude, longitude].
        "BoundingBoxes": [[[round(lat_min, 4), round(lon_min, 4)], [round(lat_max, 4), round(lon_max, 4)]]],
        "FilterMessageTypes": MESSAGE_TYPES,
    }


class KeyRejected(Exception):
    pass


async def _subscribe(ws, request: dict) -> None:
    """Send the subscription and wait for aisstream.io to confirm it."""
    await ws.send(json.dumps(request))
    try:
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), CONFIRM_TIMEOUT))
            if msg.get("MessageType") == "SubscriptionConfirmation":
                return
            if msg.get("error"):
                raise KeyRejected(str(msg["error"]))
    except websockets.ConnectionClosed:
        raise KeyRejected("aisstream.io rejected the API key") from None
    except TimeoutError:
        raise ConnectionError("aisstream.io didn't confirm the subscription") from None


async def check_key(api_key: str) -> str | None:
    """None if aisstream.io accepts the API key, else why not."""
    request = {"APIKey": api_key, "BoundingBoxes": [[[0.0, 0.0], [0.01, 0.01]]], "FilterMessageTypes": ["ShipStaticData"]}
    try:
        async with websockets.connect(URL, open_timeout=15) as ws:
            await _subscribe(ws, request)
    except KeyRejected:
        return "aisstream.io rejected that API key."
    except (OSError, ConnectionError, websockets.WebSocketException, TimeoutError) as exc:
        return f"Couldn't reach aisstream.io to check the key: {exc or type(exc).__name__}"
    return None


class AisStreamFeed:
    def __init__(self, request: dict, on_message: Callable[[dict], None], status: dict):
        self.request = request
        self.on_message = on_message
        self.status = status

    async def run(self) -> None:
        delay = 5.0
        while True:
            try:
                async with websockets.connect(URL, open_timeout=20, ping_interval=20,
                                              max_size=MAX_MESSAGE_BYTES) as ws:
                    await _subscribe(ws, self.request)
                    self.status.update(ok=True, connected=True, error=None)
                    log.info("Connected to aisstream.io")
                    delay = 5.0
                    async for raw in ws:
                        try:
                            self.on_message(json.loads(raw))
                        except Exception:
                            log.exception("Failed to process an AIS message")
                raise ConnectionError("aisstream.io closed the connection")
            except asyncio.CancelledError:
                raise
            except KeyRejected as exc:
                self.status.update(ok=False, connected=False, error=str(exc))
                log.warning("aisstream.io: %s", exc)
                delay = 300.0  # a bad key won't fix itself; don't hammer the service
            except Exception as exc:
                self.status.update(ok=False, connected=False, error=str(exc) or type(exc).__name__)
                log.warning("aisstream.io connection lost: %s", exc)
            # Back off, with jitter so many clients don't reconnect in step.
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, 300.0)
