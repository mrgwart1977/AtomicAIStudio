"""Project-scoped WebSocket event broadcaster.

Every event carries project_id, an increasing sequence, and the resource
it concerns, so reconnecting clients can reconcile from persisted state.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[WebSocket] = set()
        self._seq = itertools.count(1)
        self._lock = asyncio.Lock()

    async def subscribe(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._subscribers.add(ws)

    async def unsubscribe(self, ws: WebSocket) -> None:
        async with self._lock:
            self._subscribers.discard(ws)

    async def publish(self, event: str, project_id: str | None, resource_id: str | None, payload: dict[str, Any]) -> None:
        message = {
            "event": event,
            "seq": next(self._seq),
            "project_id": project_id,
            "resource_id": resource_id,
            "ts": time.time(),
            "payload": payload,
        }
        async with self._lock:
            targets = list(self._subscribers)
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:  # client vanished; cleanup happens on read error too
                log.debug("ws send failed", exc_info=True)
                async with self._lock:
                    self._subscribers.discard(ws)
