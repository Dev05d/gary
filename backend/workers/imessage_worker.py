"""Background iMessage polling.

Structurally different from Gmail and Calendar in the one way that matters:
there is no OAuth handshake to arrive from. The source is a local file, and
connecting is a setting (`imessage_enabled`), not a consent screen — so this
worker also owns creating the `Source` row once that setting is turned on,
which the other two connectors get for free from their OAuth callback.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.connectors.imessage.reader import IMessageUnavailable
from backend.connectors.imessage.sync import IMessageSyncOutcome, sync_imessage_once
from backend.database.models import Source, SyncState, new_id, utcnow
from backend.database.session import session_scope
from backend.events.bus import get_bus
from backend.workers.scheduling import due, record_failure

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

#: One Messages database per machine — a fixed identifier rather than an
#: account email, since there is nothing to distinguish accounts by.
ACCOUNT_IDENTIFIER = "local"


async def ensure_source(session: AsyncSession) -> Source:
    """Get or create the (singleton) iMessage source.

    There is no callback to do this from, so it happens lazily on the first
    poll tick after `imessage_enabled` is turned on — mirroring what
    `google_callback` does synchronously for Gmail/Calendar, just triggered by
    a setting instead of a redirect.

    Uses an atomic upsert rather than select-then-insert: the worker's own
    poll tick and a user clicking "Enable iMessage" can both reach here for
    the same (kind, account_identifier) pair, each on its own session, each
    having just seen no row. A plain insert after that select is a
    check-then-act race — proved concretely by interleaving two sessions by
    hand rather than hoping `asyncio.gather` happens to catch it: both see no
    row, the first commit succeeds, the second dies on the unique constraint.
    Same class of bug as the settings service and `gmail.sync.upsert_identity`
    already guard against.
    """
    await session.execute(
        sqlite_insert(Source)
        .values(
            id=new_id(),
            kind="imessage",
            display_name="iMessage (this Mac)",
            account_identifier=ACCOUNT_IDENTIFIER,
            enabled=True,
            status="connecting",
        )
        .on_conflict_do_nothing(index_elements=["kind", "account_identifier"])
    )
    source = (
        await session.execute(
            select(Source).where(
                Source.kind == "imessage", Source.account_identifier == ACCOUNT_IDENTIFIER
            )
        )
    ).scalar_one()

    if not source.enabled:
        source.enabled = True
        source.status = "connecting"
        source.last_error = None
    return source


async def sync_source(
    session: AsyncSession, *, source: Source, settings: Settings
) -> Tuple[Optional[IMessageSyncOutcome], Optional[str]]:
    """One sync pass for one source. Returns (outcome, error)."""
    if source.kind != "imessage":
        return None, f"{source.kind} sync is not implemented yet."
    if not source.enabled:
        return None, "Source is disabled."
    if not settings.imessage_enabled:
        # The master switch can be flipped off without disconnecting the
        # source, e.g. to pause without losing what has already been recorded.
        return None, "iMessage sync is turned off in Settings → Sync."

    try:
        outcome = await sync_imessage_once(
            session,
            source_id=source.id,
            db_path=settings.imessage_db_path,
            gap_minutes=settings.imessage_session_gap_minutes,
        )
        source.status = "connected"
        source.last_sync_at = utcnow()
        # Not cleared to None unconditionally: a coverage warning belongs on
        # the source row, the same place Gmail's reauth message would show up,
        # so the Sources panel surfaces it without a separate code path.
        source.last_error = outcome.coverage_warning

        if outcome.ingested or outcome.reactions:
            await get_bus().emit(
                "imessage",
                "imessage.sync.completed",
                source_id=source.id,
                ingested=outcome.ingested,
                sessions=outcome.sessions,
                reactions=outcome.reactions,
                summary=outcome.summary(),
            )
        return outcome, None

    except IMessageUnavailable as exc:
        source.status = "error"
        source.last_error = str(exc)
        await record_failure(session, source_id=source.id, error=str(exc))
        return None, str(exc)


async def poll_forever(app: "FastAPI", stop: asyncio.Event) -> None:
    """The worker loop.

    Every pass checks `settings.imessage_enabled` fresh from `app.state` — the
    same live-reload the Gmail and Calendar workers now use — so switching the
    setting on takes effect within one tick, and switching it off stops disk
    access within one tick, with no restart either way.
    """
    log.info("iMessage worker started (idle until enabled in Settings)")

    while not stop.is_set():
        settings: Settings = app.state.settings

        if settings.imessage_enabled:
            interval = timedelta(seconds=settings.imessage_poll_interval_seconds)
            try:
                async with session_scope() as session:
                    source = await ensure_source(session)
                    state = await session.get(SyncState, source.id)
                    if due(state, interval):
                        outcome, error = await sync_source(
                            session, source=source, settings=settings
                        )
                        if error:
                            log.warning("iMessage sync failed: %s", error)
                        elif outcome and outcome.ingested:
                            log.info("iMessage sync: %s", outcome.summary())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("iMessage worker iteration failed")
        else:
            # Turned off (the default): touch nothing on disk — no copy, no
            # open, nothing that reads the file holding every message on the
            # machine. A source row from a previous session where it was on
            # is left connected as far as `enabled` goes (turning the setting
            # back on should not require re-adding it), but its status is
            # corrected so the Sources panel does not keep claiming a sync
            # that stopped ticking is still "connected".
            try:
                async with session_scope() as session:
                    source = (
                        await session.execute(
                            select(Source).where(
                                Source.kind == "imessage",
                                Source.account_identifier == ACCOUNT_IDENTIFIER,
                            )
                        )
                    ).scalar_one_or_none()
                    if source is not None and source.status not in ("paused", "disconnected"):
                        source.status = "paused"
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("iMessage worker could not mark the source paused")

        tick = max(15, min(settings.imessage_poll_interval_seconds, 60))
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            continue

    log.info("iMessage worker stopped")
