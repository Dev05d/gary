from __future__ import annotations

import asyncio

from backend.events.bus import AgentEvent, EventBus


async def test_subscriber_receives_published_event():
    bus = EventBus()
    async with bus.subscribe(["*"]) as sub:
        await bus.emit("gmail", "gmail.message.received", message_id="abc")
        event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert event.type == "gmail.message.received"
    assert event.payload["message_id"] == "abc"


async def test_pattern_filtering():
    bus = EventBus()
    async with bus.subscribe(["gmail.*"]) as gmail_sub:
        await bus.emit("calendar", "calendar.event.created")
        await bus.emit("gmail", "gmail.message.received")
        event = await asyncio.wait_for(gmail_sub.queue.get(), timeout=1)
    assert event.type == "gmail.message.received"
    assert gmail_sub.queue.empty()


async def test_slow_subscriber_drops_oldest_and_never_blocks_publisher():
    """One stalled WebSocket client must not be able to stall ingestion."""
    bus = EventBus()
    async with bus.subscribe(["*"]) as sub:
        sub.queue = asyncio.Queue(maxsize=3)
        for i in range(10):
            await asyncio.wait_for(bus.emit("system", "test.event", n=i), timeout=1)
        assert sub.queue.qsize() == 3
        assert sub.dropped == 7
        newest = [(await sub.queue.get()).payload["n"] for _ in range(3)]
    assert newest == [7, 8, 9]  # oldest dropped, newest kept


async def test_unsubscribe_on_context_exit():
    bus = EventBus()
    async with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0


async def test_history_is_capped_and_ordered():
    bus = EventBus(history_size=5)
    for i in range(20):
        await bus.emit("system", "test.event", n=i)
    recent = bus.recent(50)
    assert len(recent) == 5
    assert [e.payload["n"] for e in recent] == [15, 16, 17, 18, 19]


async def test_events_are_serialisable_for_the_websocket():
    event = AgentEvent(source="gmail", type="gmail.message.received", payload={"id": "x"})
    dumped = event.model_dump(mode="json")
    assert isinstance(dumped["timestamp"], str)
    assert dumped["type"] == "gmail.message.received"
