"""Your own AIS receiver, sending NMEA sentences over the network.

Two ways to connect, set as an address:

- `udp://0.0.0.0:10110`: Passerby listens, and the receiver sends to it. With AIS-catcher:
  `AIS-catcher -u <passerby's address> 10110`. In Docker, publish the port as UDP.
- `tcp://192.168.1.50:5012`: Passerby connects to the receiver, which serves NMEA over TCP.
  With AIS-catcher: `AIS-catcher -S 5012`. Nothing to open in Docker.

Anything else that sends standard !AIVDM sentences works too (rtl-ais, dAISy, chart plotters,
OpenCPN's NMEA output...).
"""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from .nmea import NmeaDecoder

log = logging.getLogger(__name__)

READ_TIMEOUT = 15 * 60  # a quiet TCP connection is reopened after this long


def parse_address(address: str) -> tuple[str, str, int]:
    """'udp://0.0.0.0:10110' -> ('udp', '0.0.0.0', 10110). Raises ValueError if it isn't one."""
    parts = urlsplit(address.strip())
    if parts.scheme not in ("udp", "tcp"):
        raise ValueError("The AIS receiver address must start udp:// or tcp://")
    try:
        port = parts.port
    except ValueError:
        port = None
    if not port:
        raise ValueError("The AIS receiver address needs a port, like udp://0.0.0.0:10110")
    host = parts.hostname or ("0.0.0.0" if parts.scheme == "udp" else "")
    if not host:
        raise ValueError("A tcp:// address needs the receiver's host, like tcp://192.168.1.50:5012")
    return parts.scheme, host, port


class AisReceiver:
    def __init__(self, address: str, on_message: Callable[[dict], None], status: dict):
        self.kind, self.host, self.port = parse_address(address)
        self.on_message = on_message
        self.status = status
        self.decoder = NmeaDecoder()
        status.update(ok=None, connected=False, messages=0, last_message=None, error=None,
                      address=f"{self.kind}://{self.host}:{self.port}")

    def handle_line(self, line: str) -> None:
        try:
            msg = self.decoder.feed(line)
        except Exception:
            log.exception("Failed to decode an AIS sentence")
            return
        if msg is None:
            return
        self.status["messages"] += 1
        self.status["last_message"] = time.time()
        self.status["ok"] = True
        try:
            self.on_message(msg)
        except Exception:
            log.exception("Failed to process an AIS message")

    def handle_data(self, data: bytes) -> None:
        for line in data.decode("ascii", "replace").splitlines():
            if line.strip():
                self.handle_line(line)

    async def run(self) -> None:
        await (self._listen_udp() if self.kind == "udp" else self._connect_tcp())

    async def _listen_udp(self) -> None:
        receiver = self

        class Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                receiver.handle_data(data)

        while True:
            try:
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    Protocol, local_addr=(self.host, self.port))
            except OSError as exc:
                self.status.update(ok=False, connected=False, error=f"Can't listen on UDP port {self.port}: {exc}")
                log.warning("AIS receiver: %s", self.status["error"])
                await asyncio.sleep(60)
                continue
            self.status.update(connected=True, error=None)
            log.info("Listening for AIS on UDP %s:%s", self.host, self.port)
            try:
                await asyncio.Event().wait()  # until cancelled
            finally:
                transport.close()

    async def _connect_tcp(self) -> None:
        delay = 5.0
        while True:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 20)
                self.status.update(connected=True, error=None)
                log.info("Connected to the AIS receiver at %s:%s", self.host, self.port)
                delay = 5.0
                while line := await asyncio.wait_for(reader.readline(), READ_TIMEOUT):
                    self.handle_data(line)
                raise ConnectionError("the receiver closed the connection")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    error = "nothing received for 15 minutes" if self.status["connected"] else "couldn't connect"
                else:
                    error = str(exc) or type(exc).__name__
                self.status.update(ok=False, connected=False, error=f"AIS receiver: {error}")
                log.warning("AIS receiver at %s:%s: %s", self.host, self.port, error)
            finally:
                if writer:
                    writer.close()
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, 300.0)
