"""Session-token auth for the loopback API.

The desktop shell generates a token per launch and injects it into the UI.
Requests must present it as the X-Atomic-Token header; the WebSocket and
image/file endpoints also accept ?token= because browser tags cannot set
headers. /health stays open so the shell can poll readiness.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, WebSocket, status

from app.config import Settings


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def require_token(request: Request, settings: Settings = Depends(get_app_settings)) -> None:
    token = request.headers.get("x-atomic-token") or request.query_params.get("token")
    if token != settings.session_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session token")


async def require_token_ws(websocket: WebSocket) -> None:
    settings: Settings = websocket.app.state.settings
    token = websocket.query_params.get("token")
    if token != settings.session_token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
