"""Protection for an app with no user accounts, meant for a home network.

- An optional admin password (ADMIN_PASSWORD) guards everything that changes the app:
  the settings page and the receiver lookup. The map stays open to anyone on the network.
  Signing in sets a cookie, signed with a key kept in the data folder, that lasts 30 days.
- Requests must name a host that's plainly local (an IP address, localhost, .local, .lan,
  .home.arpa, .internal) or one listed in ALLOWED_HOSTS. That stops DNS rebinding, where
  a web page you visit renames itself to your server's address to talk to it. Tailscale
  names (.ts.net) are allowed too.
- Changes and the live WebSocket must come from the app's own pages, not another site.
- Every response gets headers that stop other sites framing the app, MIME sniffing and
  scripts from anywhere but the app itself and the map library's CDN.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "tt_admin"
SESSION_SECONDS = 30 * 86400
# Wrong passwords from one address: this many within this long locks it out until they age.
MAX_FAILURES = 10
LOCKOUT_SECONDS = 300
# Tailscale names (.ts.net) only resolve inside your own tailnet, so they count as local too.
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain", ".ts.net")
# Methods that change something; these must come from the app's own pages.
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://unpkg.com",
    # Leaflet positions everything with inline styles.
    "style-src 'self' https://unpkg.com 'unsafe-inline'",
    # Map tiles and aircraft photos come from elsewhere.
    "img-src 'self' data: https:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])
HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    # OpenStreetMap's tile servers refuse requests without a Referer, so send the origin (the
    # browser default) rather than nothing.
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
]


def host_name(host: str) -> str:
    """The name part of a Host header or URL netloc ("[::1]:8000" -> "::1")."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(host: str, allowed: list[str]) -> bool:
    if "*" in allowed:
        return True
    name = host_name(host)
    if not name:
        return False
    if name in allowed or name == "localhost" or name.endswith(LOCAL_SUFFIXES):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def same_origin(origin: str | None, host: str | None) -> bool:
    """True when there's no Origin (not a browser) or it's the host the request was sent to."""
    if not origin:
        return True
    if not host or origin == "null":
        return False
    return urlsplit(origin).netloc.lower() == host.strip().lower()


class SecurityMiddleware:
    """Host check, cross-site check and security headers, for HTTP and WebSocket alike."""

    def __init__(self, app: ASGIApp, allowed_hosts: list[str]):
        self.app = app
        self.allowed_hosts = [h.strip().lower() for h in allowed_hosts if h.strip()]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        reason = None
        if not host_allowed(host, self.allowed_hosts):
            reason = f"Host {host_name(host)!r} is not allowed. Add it to ALLOWED_HOSTS in .env."
        elif ((scope["type"] == "websocket" or scope["method"] in UNSAFE_METHODS)
              and not same_origin(headers.get("origin"), host)):
            reason = "Cross-site request refused."
        if reason:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            body = reason.encode()
            await send({"type": "http.response.start", "status": 403, "headers": [
                (b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + HEADERS
            await send(message)

        await self.app(scope, receive, with_headers if scope["type"] == "http" else send)


def server_key(data_dir: Path) -> bytes:
    """A random key, made once and kept in the data folder, for signing sign-in cookies."""
    path = data_dir / "secret.key"
    try:
        return bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.hex())
    return key


class AdminAuth:
    def __init__(self, password: str, data_dir: Path):
        self.password = password
        # Changing the password signs everyone out.
        self._key = hmac.new(server_key(data_dir), password.encode(), hashlib.sha256).digest() if password else b""
        self._failures: dict[str, list[float]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def _sign(self, expires: int) -> str:
        return hmac.new(self._key, str(expires).encode(), hashlib.sha256).hexdigest()

    def new_session(self, now: float | None = None) -> str:
        expires = int((time.time() if now is None else now) + SESSION_SECONDS)
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token: str | None, now: float | None = None) -> bool:
        if not self.enabled:
            return True
        try:
            expires, signature = (token or "").split(".", 1)
            fresh = int(expires) > (time.time() if now is None else now)
        except ValueError:
            return False
        return fresh and hmac.compare_digest(signature, self._sign(int(expires)))

    async def check_password(self, password: str, client: str) -> bool:
        """Constant-time check; after repeated failures from one address, slow down, then lock out."""
        now = time.time()
        recent = [t for t in self._failures.get(client, []) if now - t < LOCKOUT_SECONDS]
        if len(recent) >= MAX_FAILURES:
            raise HTTPException(429, "Too many wrong passwords. Try again in a few minutes.")
        if hmac.compare_digest(password.encode(), self.password.encode()):
            self._failures.pop(client, None)
            return True
        recent.append(now)
        self._failures[client] = recent
        await asyncio.sleep(min(2 ** (len(recent) - 1), 30))  # 1, 2, 4, ... seconds
        return False

    def require(self, request: Request) -> None:
        if not self.valid(request.cookies.get(COOKIE)):
            raise HTTPException(401, "Sign in with the admin password to change settings.")
