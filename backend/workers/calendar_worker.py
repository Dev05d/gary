"""Background Google Calendar polling.

Structurally the same shape as the Gmail worker — same OAuth credential (the
scopes requested at connect time cover both), same polling loop, same
backoff — but the sync itself works over a window rather than a watermark. See
`backend/connectors/calendar/client.py` for why.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.connectors.calendar.client import CalendarAuthError, CalendarClient, CalendarError
from backend.connectors.calendar.sync import CalendarSyncOutcome, sync_calendar_once
from backend.database.models import OAuthCredential, Source, SyncState, utcnow
from backend.database.session import session_scope
from backend.events.bus import get_bus
from backend.security.crypto import EncryptionError, get_cipher
from backend.workers.scheduling import due, record_failure

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)


async def _access_token(
    session: AsyncSession, source: Source, settings: Settings
) -> Tuple[Optional[str], Optional[str]]:
    """Same credential Gmail uses — `calendar.readonly` was requested alongside
    `gmail.readonly` in the one consent screen, so there is nothing separate to
    connect."""
    from backend.connectors.gmail import oauth

    credential = (
        await session.execute(
            select(OAuthCredential).where(
                OAuthCredential.provider == "google",
                OAuthCredential.account_email == (source.account_identifier or ""),
            )
        )
    ).scalar_one_or_none()

    if credential is None:
        return None, "No stored credential. Reconnect the Google account from Sources."

    try:
        refresh_token = get_cipher(settings.credential_encryption_key).decrypt(
            credential.encrypted_token, aad=credential.account_email
        )
    except EncryptionError as exc:
        return None, str(exc)

    try:
        bundle = await oauth.refresh_access_token(
            refresh_token=refresh_token,
            client_id=settings.google_client_id or "",
            client_secret=settings.google_client_secret or "",
        )
    except oauth.ReauthRequired as exc:
        source.status = "reauth_required"
        source.last_error = str(exc)
        return None, str(exc)
    except oauth.OAuthError as exc:
        return None, str(exc)

    credential.expires_at = bundle.expires_at
    credential.updated_at = utcnow()
    return bundle.access_token, None


async def sync_source(
    session: AsyncSession, *, source: Source, settings: Settings
) -> Tuple[Optional[CalendarSyncOutcome], Optional[str]]:
    """One sync pass for one source. Returns (outcome, error)."""
    if source.kind != "gcal":
        return None, f"{source.kind} sync is not implemented yet."
    if not source.enabled:
        return None, "Source is disabled."

    token, error = await _access_token(session, source, settings)
    if error:
        await record_failure(session, source_id=source.id, error=error)
        return None, error

    client = CalendarClient(token or "", timeout=settings.ollama_timeout)
    try:
        outcome = await sync_calendar_once(
            session,
            client,
            source_id=source.id,
            past_days=settings.calendar_past_days,
            future_days=settings.calendar_future_days,
        )
        source.status = "connected"
        source.last_sync_at = utcnow()
        source.last_error = None

        if outcome.created or outcome.updated or outcome.cancelled:
            await get_bus().emit(
                "gcal",
                "calendar.sync.completed",
                source_id=source.id,
                created=outcome.created,
                updated=outcome.updated,
                cancelled=outcome.cancelled,
                summary=outcome.summary(),
            )
        return outcome, None

    except CalendarAuthError as exc:
        source.status = "reauth_required"
        source.last_error = str(exc)
        await record_failure(session, source_id=source.id, error=str(exc))
        return None, str(exc)
    except CalendarError as exc:
        source.last_error = str(exc)
        await record_failure(session, source_id=source.id, error=str(exc))
        return None, str(exc)
    finally:
        await client.aclose()


async def poll_forever(app: "FastAPI", stop: asyncio.Event) -> None:
    """The worker loop.

    Settings are re-read from `app.state` on every pass, not captured once at
    startup — a poll-interval or window change made in the Settings UI takes
    effect on the next tick rather than needing a restart nobody is told about.
    """
    log.info("Calendar worker started")

    while not stop.is_set():
        settings: Settings = app.state.settings
        interval = timedelta(seconds=settings.calendar_poll_interval_seconds)

        try:
            async with session_scope() as session:
                sources = (
                    await session.execute(
                        select(Source).where(
                            Source.kind == "gcal",
                            Source.enabled.is_(True),
                            Source.status.in_(("connected", "syncing")),
                        )
                    )
                ).scalars().all()

                for source in sources:
                    state = await session.get(SyncState, source.id)
                    if not due(state, interval):
                        continue
                    outcome, error = await sync_source(
                        session, source=source, settings=settings
                    )
                    if error:
                        log.warning("calendar sync failed for %s: %s", source.id, error)
                    elif outcome and outcome.summary() != "no changes":
                        log.info("calendar sync: %s", outcome.summary())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("calendar worker iteration failed")

        tick = max(15, min(settings.calendar_poll_interval_seconds, 60))
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            continue

    log.info("Calendar worker stopped")
