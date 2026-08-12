"""WebSocket feed of the event bus.

Carries system events — ingestion progress, sync status, notifications — to the
UI. Chat streaming does *not* go through here (see routes_chat.py).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from backend.config import get_settings
from backend.events.bus import get_bus

log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

HEARTBEAT_SECONDS = 25


@router.websocket("/ws/events")
async def events_socket(websocket: WebSocket, token: str = Query(default="")) -> None:
    settings = getattr(websocket.app.state, "settings", None) or get_settings()
    # Browsers cannot set headers on a WebSocket handshake, so the token rides
    # in the query string. It never leaves localhost by default.
    if settings.api_auth_token and token != settings.api_auth_token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    bus = get_bus()

    try:
        async with bus.subscribe(["*"]) as subscription:
            await websocket.send_json(
                {
                    "type": "connection.established",
                    "payload": {"recent": [e.model_dump(mode="json") for e in bus.recent(20)]},
                }
            )
            while True:
                try:
                    event = await asyncio.wait_for(
                        subscription.queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # Keeps intermediaries from reaping an idle socket.
                    await websocket.send_json({"type": "heartbeat", "payload": {}})
                    continue
                await websocket.send_json(event.model_dump(mode="json"))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("websocket event feed failed")
        try:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except RuntimeError:
            pass
