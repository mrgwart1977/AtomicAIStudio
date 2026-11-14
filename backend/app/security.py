"""Session-token auth for the loopback API.

The desktop shell generates a token per launch and injects it into the UI.
Requests must present it as the X-Atomic-Token header — never in a URL.
/health stays open so the shell can poll readiness.

A2.1 — WebSocket first-frame authentication: every client sends
``{"type":"auth","token":...}`` as its FIRST frame inside a strict
timeout, under a bounded unauthenticated connection pool. No
subscription or event delivery exists before that point; the token is
never logged.

A3 — query-token authentication is fully removed: no endpoint accepts
``?token=`` anymore (REST is header-only; the WS requires the auth
frame regardless of query parameters).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def require_token(request: Request, settings: Settings = Depends(get_app_settings)) -> None:
    token = request.headers.get("x-atomic-token")
    if token != settings.session_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session token")


# A2.1 first-frame WS auth limits.
WS_AUTH_TIMEOUT_S = 3.0          # strict deadline for the auth frame
WS_AUTH_MAX_BYTES = 4096         # auth frame size bound
WS_MAX_UNAUTHENTICATED = 32      # bounded unauthenticated pool

#: Origin hosts permitted to open the loopback WebSocket. Covers the
#: packaged WebView2 origin (http://tauri.localhost on Windows) and
#: loopback dev tools (vite dev :1420, CDP diagnostics, scripts). Any
#: other host — e.g. a website loaded in a browser attempting a
#: cross-site WebSocket to the sidecar — is refused. Defense in depth:
#: the token remains required regardless of origin.
_WS_ALLOWED_ORIGIN_HOSTS = frozenset({
    "tauri.localhost", "localhost", "127.0.0.1", "::1"})


def ws_origin_allowed(origin: str | None) -> bool:
    """Origin policy for the loopback WS endpoint (A2.1).

    A browser always sends Origin — an untrusted website attempting a
    cross-site WebSocket to the sidecar is refused before accept. A
    missing header means a non-browser client (the token still gates);
    the literal ``"null"`` opaque-origin marker is refused.
    """
    if origin is None or not origin.strip():
        return True
    if origin.strip().lower() == "null":
        return False
    from urllib.parse import urlparse
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    if (parsed.hostname or "").lower() not in _WS_ALLOWED_ORIGIN_HOSTS:
        return False
    # Exact host match only — no suffix/substring. Any port on an
    # allowed host is permitted (dev tools bind random ports) but an
    # unparseable/out-of-range port makes the origin deceptive — refuse.
    try:
        parsed.port
    except ValueError:
        return False
    return True


def parse_auth_frame(raw: str) -> str | None:
    """Validate the first-frame auth payload; return the token or None.

    Only ``{"type":"auth","token":<nonempty string>}`` is an auth frame.
    Anything else — non-JSON, wrong shape, missing/empty token — is not
    an auth frame; the caller treats it as protocol refusal. The token
    value is never logged anywhere.
    """
    import json
    try:
        frame = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if (isinstance(frame, dict) and frame.get("type") == "auth"
            and isinstance(frame.get("token"), str)
            and frame["token"]):
        return frame["token"]
    return None


def is_auth_frame(raw: str) -> bool:
    """True when a text frame is shaped like an auth attempt — used to
    reject duplicate authentication on an established connection."""
    import json
    try:
        frame = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(frame, dict) and frame.get("type") == "auth"
