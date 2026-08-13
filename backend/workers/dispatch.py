"""Routes a `Source` to the worker module that knows how to sync it.

One place for this mapping, used by both the manual "check now" API route and
anything else that needs to sync a source without caring which kind it is.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.database.models import Source


async def sync_source(
    session: AsyncSession, *, source: Source, settings: Settings
) -> Tuple[Optional[Any], Optional[str]]:
    """One sync pass for one source, dispatched by `source.kind`.

    Imports are local to the branch actually taken rather than hoisted to the
    top of the module — each connector pulls in its own client and crypto
    dependencies, and there is no reason to pay for all three when only one
    kind of source is ever being synced in a given call.
    """
    if source.kind == "gmail":
        from backend.workers.gmail_worker import sync_source as _sync

        return await _sync(session, source=source, settings=settings)
    if source.kind == "gcal":
        from backend.workers.calendar_worker import sync_source as _sync

        return await _sync(session, source=source, settings=settings)
    if source.kind == "imessage":
        from backend.workers.imessage_worker import sync_source as _sync

        return await _sync(session, source=source, settings=settings)
    return None, f"{source.kind} sync is not implemented yet."
