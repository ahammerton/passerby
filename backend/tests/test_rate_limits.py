import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import main
from app.sources import ADSBLOL_MIN_INTERVAL, CommunitySource, RateLimited
from app.sources.community import rate_limited

# What adsb.lol sent once a connection had used its day's requests (November 2026).
ADSBLOL_429 = {"x-ratelimit-limit": "500, 500;w=86400", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "28149"}


def test_daily_limit_parsed():
    exc = rate_limited(httpx.Response(429, headers=ADSBLOL_429))
    assert str(exc) == "Daily limit reached (500 requests a day)" and exc.retry_after == 28149
    other = rate_limited(httpx.Response(429, headers={"retry-after": "30"}))
    assert str(other) == "Rate limited" and other.retry_after == 30
    assert rate_limited(httpx.Response(429)).retry_after is None


def test_community_source_raises_rate_limited():
    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers=ADSBLOL_429)))
        src = CommunitySource(client, name="adsblol", base_url="https://api.example", lat=51.5, lon=-0.1,
                              radius_km=150, interval=200)
        try:
            await src.fetch()
        finally:
            await client.aclose()

    with pytest.raises(RateLimited) as info:
        asyncio.run(scenario())
    assert info.value.retry_after == 28149


def poll_once(monkeypatch, error):
    """Run the poller until its first sleep; return (sleep seconds, status)."""
    class Stop(Exception):
        pass

    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        raise Stop

    async def fetch():
        raise error

    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    source = SimpleNamespace(name="adsblol", local=False, interval=200, priority=5, fetch=fetch)
    status = {"ok": True, "last_update": None, "count": 3, "error": None}
    state = SimpleNamespace(settings=SimpleNamespace(online_feeds="fill"), sources=[source],
                            source_status={"adsblol": status}, aggregator=None)
    with pytest.raises(Stop):
        asyncio.run(main.poll_source(state, source))
    return slept[0], status


def test_poller_waits_for_reset(monkeypatch):
    wait, status = poll_once(monkeypatch, RateLimited("Daily limit reached (500 requests a day)", 28149))
    assert wait == 28149 + 60
    assert status["ok"] is False and status["resumes_at"]
    assert status["error"].startswith("Daily limit reached (500 requests a day); resumes at ")


def test_poller_backs_off_without_reset_time(monkeypatch):
    wait, status = poll_once(monkeypatch, RateLimited("Rate limited"))
    assert wait == 400  # interval plus a first back-off of one interval
    assert status["error"] == "Rate limited; trying again in 7 min" and not status.get("resumes_at")


def test_short_adsblol_interval_raised():
    settings = main.get_settings().model_copy(update={
        "adsblol_enabled": True, "adsblol_interval": 30, "readsb_enabled": False, "opensky_enabled": False})
    [src] = [s for s in main.build_sources(settings, httpx.AsyncClient()) if s.name == "adsblol"]
    assert src.interval == ADSBLOL_MIN_INTERVAL
