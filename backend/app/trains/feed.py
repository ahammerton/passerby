"""STOMP connection to Network Rail's public data feeds (TRUST train movements).

stomp.py runs its own receiver thread; messages are handed straight to the
tracker. The asyncio supervisor reconnects if the connection drops.
"""

import asyncio
import json
import logging
from collections.abc import Callable

import stomp

log = logging.getLogger(__name__)

HOST = ("publicdatafeeds.networkrail.co.uk", 61618)
TOPIC = "/topic/TRAIN_MVT_ALL_TOC"


class _Listener(stomp.ConnectionListener):
    def __init__(self, on_messages: Callable[[list[dict]], None], status: dict):
        self.on_messages = on_messages
        self.status = status

    def on_message(self, frame):
        try:
            self.on_messages(json.loads(frame.body))
        except Exception:
            log.exception("Failed to process a TRUST frame")

    def on_error(self, frame):
        log.warning("Network Rail feed error: %s", frame.body[:200])
        self.status.update(ok=False, error=frame.body[:200])

    def on_disconnected(self):
        self.status.update(connected=False)


class TrustFeed:
    def __init__(self, username: str, password: str, on_messages: Callable[[list[dict]], None], status: dict):
        self.username = username
        self.password = password
        self.on_messages = on_messages
        self.status = status
        self.conn: stomp.Connection12 | None = None

    def _connect(self) -> None:
        conn = stomp.Connection12([HOST], keepalive=True, heartbeats=(15000, 15000))
        conn.set_listener("", _Listener(self.on_messages, self.status))
        conn.connect(username=self.username, passcode=self.password, wait=True)
        conn.subscribe(destination=TOPIC, id=1, ack="auto")
        self.conn = conn
        self.status.update(ok=True, connected=True, error=None)
        log.info("Connected to Network Rail TRUST feed")

    async def run(self) -> None:
        delay = 5
        try:
            while True:
                if not (self.conn and self.conn.is_connected()):
                    try:
                        await asyncio.to_thread(self._connect)
                        delay = 5
                    except Exception as exc:
                        log.warning("Network Rail feed connection failed: %s", exc)
                        self.status.update(ok=False, connected=False, error=str(exc) or type(exc).__name__)
                        # Back off so a bad login doesn't hammer the server.
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 300)
                        continue
                await asyncio.sleep(5)
        finally:
            if self.conn and self.conn.is_connected():
                await asyncio.to_thread(self.conn.disconnect)
