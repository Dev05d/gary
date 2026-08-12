"""Background Gmail polling.

Polling rather than Pub/Sub, deliberately: push needs a public HTTPS endpoint
Google can reach, which on a laptop means running a tunnel and letting a third
party see your notification traffic. `history.list` against a watermark is one
cheap call that returns nothing when idle.

Scheduling is **interval-since-last-success**, not wall-clock. A laptop that
slept through a cron tick would silently skip it; this notices the gap on wake
and catches up.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.connectors.gmail import oauth
from backend.connectors.gmail.client import GmailAuthError, GmailClient, GmailError
from backend.connectors.gmail.sync import SyncOutcome, record_failure, sync_once
from backend.database.models import Identity, OAuthCredential, Source, SyncState, utcnow
from backend.database.session import session_scope
from backend.events.bus import get_bus
from backend.pipeline.label_policy import LabelPolicy
from backend.security.crypto import EncryptionError, get_cipher

log = logging.getLogger(__name__)

#: Back off after repeated failures rather than hammering a broken account.
MAX_BACKOFF = timedelta(minutes=30)


async def my_addresses(session: AsyncSession) -> Set[str]:
    """The user's own addresses, derived from what has been seen in SENT.

    Seeding this from the connected account alone misses send-as aliases and
    custom domains — and a missed alias makes "who haven't I replied to" wrong
    from the first message, because your own replies count as inbound.
    """
    rows = (
        await session.execute(select(Identity.value_normalized).where(Identity.is_me.is_(True)))
    ).scalars().all()
    accounts = (
        await session.execute(
            select(Source.account_identifier).where(Source.account_identifier.isnot(None))
        )
    ).scalars().all()
    return {r for r in rows if r} | {a.lower() for a in accounts if a}


async def _access_token(
    session: AsyncSession, source: Source, settings: Settings
) -> Tuple[Optional[str], Optional[str]]:
    """Refresh and return an access token, or an error to report."""
    credential = (
        await session.execute(
            select(OAuthCredential).where(
                OAuthCredential.provider == "google",
                OAuthCredential.account_email == (source.account_identifier or ""),
            )
        )
    ).scalar_one_or_none()

    if credential is None:
        return None, "No stored credential. Reconnect the account."

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
        # Terminal. Retrying invalid_grant never succeeds.
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
) -> Tuple[Optional[SyncOutcome], Optional[str]]:
    """One sync pass for one source. Returns (outcome, error)."""
    if source.kind != "gmail":
        return None, f"{source.kind} sync is not implemented yet."
    if not source.enabled:
        return None, "Source is disabled."

    token, error = await _access_token(session, source, settings)
    if error:
        await record_failure(session, source_id=source.id, error=error)
        return None, error

    policy = LabelPolicy.from_settings(
        settings.gmail_label_mode,
        settings.gmail_include_labels,
        settings.gmail_exclude_labels,
    )
    client = GmailClient(token or "", timeout=settings.ollama_timeout)
    try:
        outcome = await sync_once(
            session,
            client,
            source_id=source.id,
            policy=policy,
            my_addresses=await my_addresses(session),
            mirror_deletions=settings.mirror_upstream_deletions,
        )
        source.status = "connected"
        source.last_sync_at = utcnow()
        source.last_error = None

        if outcome.ingested or outcome.deleted:
            await get_bus().emit(
                "gmail",
                "gmail.sync.completed",
                source_id=source.id,
                ingested=outcome.ingested,
                deleted=outcome.deleted,
                summary=outcome.summary(),
            )
        return outcome, None

    except GmailAuthError as exc:
        source.status = "reauth_required"
        source.last_error = str(exc)
        await record_failure(session, source_id=source.id, error=str(exc))
        return None, str(exc)
    except GmailError as exc:
        source.last_error = str(exc)
        await record_failure(session, source_id=source.id, error=str(exc))
        return None, str(exc)
    finally:
        await client.aclose()


def _due(state: Optional[SyncState], interval: timedelta) -> bool:
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


async def poll_forever(settings: Settings, stop: asyncio.Event) -> None:
    """The worker loop.

    Runs in the same process as the API. Ingestion is I/O-bound and the volume
    is a day's mail, so a separate process would buy nothing and cost the
    single-writer guarantee that keeps SQLite contention away.
    """
    log.info("Gmail worker started (checking every %ss)", settings.gmail_poll_interval_seconds)
    interval = timedelta(seconds=settings.gmail_poll_interval_seconds)
    tick = max(15, min(settings.gmail_poll_interval_seconds, 60))

    while not stop.is_set():
        try:
            async with session_scope() as session:
                sources = (
                    await session.execute(
                        select(Source).where(
                            Source.kind == "gmail",
                            Source.enabled.is_(True),
                            Source.status.in_(("connected", "syncing")),
                        )
                    )
                ).scalars().all()

                for source in sources:
                    state = await session.get(SyncState, source.id)
                    if not _due(state, interval):
                        continue
                    outcome, error = await sync_source(
                        session, source=source, settings=settings
                    )
                    if error:
                        log.warning("gmail sync failed for %s: %s", source.id, error)
                    elif outcome and (outcome.ingested or outcome.deleted):
                        log.info("gmail sync: %s", outcome.summary())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("gmail worker iteration failed")

        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            continue

    log.info("Gmail worker stopped")
