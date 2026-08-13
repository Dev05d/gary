"""Shared by every source's poll loop: is a sync due, and how to record one
that failed before producing an outcome to record.

Kept here rather than duplicated per-worker (Gmail's version was the
original) or imported cross-module by its private name (which is what the
first pass of the Calendar and iMessage workers did — a leading underscore on
another module's function is a sign the code belongs in its own module).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import SyncState, utcnow

#: Back off after repeated failures rather than hammering a broken account or
#: a Mac where Full Disk Access was never granted.
MAX_BACKOFF = timedelta(minutes=30)


def due(state: Optional[SyncState], interval: timedelta) -> bool:
    """Interval since last success, with backoff on repeated failure."""
    if state is None or state.last_attempt_at is None:
        return True
    last = state.last_attempt_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    wait = interval
    if state.consecutive_failures:
        wait = min(interval * (2 ** min(state.consecutive_failures, 6)), MAX_BACKOFF)
    return datetime.now(timezone.utc) - last >= wait


async def record_failure(session: AsyncSession, *, source_id: str, error: str) -> None:
    """Move the failure counters even when a sync attempt never reached the
    point of producing an outcome (an auth failure before the first request,
    a missing database before the first read).

    Creates the `SyncState` row if one does not exist yet, unlike a get-only
    lookup that silently drops the very first failure a source ever has —
    which would otherwise leave `last_error` empty and the Sources panel
    reporting a connected-looking source that has in fact never synced.
    """
    state = await session.get(SyncState, source_id)
    now = utcnow()
    if state is None:
        session.add(
            SyncState(
                source_id=source_id,
                watermark={},
                last_attempt_at=now,
                last_error=error[:1000],
                consecutive_failures=1,
                updated_at=now,
            )
        )
    else:
        state.last_attempt_at = now
        state.last_error = error[:1000]
        state.consecutive_failures = (state.consecutive_failures or 0) + 1
