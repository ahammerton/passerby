"""Checking logins before they're saved from the settings page, so a typo can't break a feed."""

import httpx

from .sources.opensky import TOKEN_URL
from .trains.corpus import CORPUS_URL


async def check_nrod(client: httpx.AsyncClient, username: str, password: str) -> str | None:
    """None if Network Rail accepts the login, else why not."""
    try:
        # A good login is redirected to the download; a bad one gets 401. Don't follow the redirect.
        resp = await client.get(CORPUS_URL, auth=(username, password), follow_redirects=False, timeout=20)
    except httpx.HTTPError as exc:
        return f"Couldn't reach Network Rail to check the login: {exc}"
    if resp.status_code in (401, 403):
        return "Network Rail rejected that email and password."
    if resp.status_code not in (200, 302):
        return f"Network Rail answered {resp.status_code} when checking the login."
    return None


async def check_opensky(client: httpx.AsyncClient, client_id: str, client_secret: str) -> str | None:
    """None if OpenSky issues a token for these API client credentials, else why not."""
    try:
        resp = await client.post(TOKEN_URL, timeout=20, data={
            "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
    except httpx.HTTPError as exc:
        return f"Couldn't reach OpenSky to check the API client: {exc}"
    if resp.status_code in (400, 401, 403):
        return "OpenSky rejected that client ID and secret."
    if resp.status_code != 200:
        return f"OpenSky answered {resp.status_code} when checking the API client."
    return None
