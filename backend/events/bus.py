"""In-process async event bus (spec §17).

Every connector emits normalised `AgentEvent`s onto this bus; workers and the
WebSocket layer subscribe.  Deliberately in-process for now — moving to Redis
later means reimplementing `publish`/`subscribe` and nothing else.

Subscribers get bounded queues and are dropped-oldest on overflow, so one slow
WebSocket client can never stall ingestion.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class AgentEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    source: str  # "gmail" | "calendar" | "system" | "chat" | ...
    type: str  # dotted: "gmail.message.received"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: Dict[str, Any] = Field(default_factory=dict)


class Subscription:
    def __init__(self, patterns: List[str], maxsize: int = 256) -> None:
        self.patterns = patterns or ["*"]
        self.queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def matches(self, event_type: str) -> bool:
        return any(fnmatch.fnmatch(event_type, p) for p in self.patterns)

    def offer(self, event: AgentEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()  # drop oldest
                self.queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                pass
            self.dropped += 1

    async def __aiter__(self) -> AsyncIterator[AgentEvent]:
        while True:
            yield await self.queue.get()


class EventBus:
    def __init__(self, history_size: int = 200) -> None:
        self._subs: List[Subscription] = []
        self._history: List[AgentEvent] = []
        self._history_size = history_size
        self._lock = asyncio.Lock()

    async def publish(self, event: AgentEvent) -> AgentEvent:
        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_size:
                self._history = self._history[-self._history_size :]
            targets = [s for s in self._subs if s.matches(event.type)]
        for sub in targets:
            sub.offer(event)
        log.debug("event %s from %s", event.type, event.source)
        return event

    async def emit(self, source: str, type_: str, **payload: Any) -> AgentEvent:
        return await self.publish(AgentEvent(source=source, type=type_, payload=payload))

    @asynccontextmanager
    async def subscribe(
        self, patterns: Optional[List[str]] = None
    ) -> AsyncIterator[Subscription]:
        sub = Subscription(patterns or ["*"])
        async with self._lock:
            self._subs.append(sub)
        try:
            yield sub
        finally:
            async with self._lock:
                if sub in self._subs:
                    self._subs.remove(sub)

    def recent(self, limit: int = 50) -> List[AgentEvent]:
        return self._history[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


_bus: Optional[EventBus] = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_bus() -> None:
    """Test hook."""
    global _bus
    _bus = None
