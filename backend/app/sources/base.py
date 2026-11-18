"""Interface every aircraft data source implements.

To add a new feed (ADS-B Exchange, airplanes.live, ...), subclass
AircraftSource, implement fetch(), and register it in main.build_sources().
"""

from abc import ABC, abstractmethod

import httpx

from ..models import Aircraft


class RateLimited(Exception):
    """The feed refused because a quota is used up. `retry_after` is how long until it resets, in
    seconds, when the feed says; the poller waits that long instead of retrying."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AircraftSource(ABC):
    #: Short identifier stored on each Aircraft and shown in the UI.
    name: str
    #: Seconds between fetches.
    interval: float
    #: Higher wins when two sources report the same aircraft.
    priority: int = 0
    #: Your own receiver, as opposed to an online feed.
    local: bool = False

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    @abstractmethod
    async def fetch(self) -> list[Aircraft]:
        """Return the current aircraft. Raise on failure; the caller logs and retries."""
