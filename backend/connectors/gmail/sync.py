"""Gmail sync orchestration.

Live-only: a watermark is recorded when the account connects, and only what
arrives after it is ingested.

Three rules govern this module, each corresponding to a silent failure:

1. **The watermark advances only after every page is persisted.** Advancing
   per page permanently skips everything on the pages that follow, and nothing
   errors. See `collect_history`.
2. **History expiry does not trigger a full sync.** Google's documented remedy
   would import the entire mailbox — precisely what live-only exists to avoid.
   A date-bounded catch-up bridges the gap instead.
3. **Ingest is idempotent.** `(source_id, source_message_id)` is unique, so a
   replay after a crash is a no-op rather than a duplicate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.gmail.client import (
    GmailAuthError,
    GmailClient,
    GmailError,
    HistoryExpired,
    collect_history,
)
from backend.connectors.gmail.parser import ParsedMessage, parse_message
from backend.database.models import (
    Identity,
    Message,
    MessageThread,
    SyncState,
    new_id,
    utcnow,
)
from backend.pipeline.identity import (
    IdentityKind,
    is_bulk_sender,
    is_role_account,
    merge_display_names,
    normalize_email,
)
from backend.pipeline.label_policy import LabelPolicy

log = logging.getLogger(__name__)


@dataclass
class SyncOutcome:
    ingested: int = 0
    updated: int = 0
    deleted: int = 0
    skipped_by_policy: int = 0
    errors: List[str] = field(default_factory=list)
    watermark: Optional[str] = None
    used_recovery: bool = False
    #: True when the run reached a clean end and the watermark may be stored.
    complete: bool = False

    def summary(self) -> str:
        bits = [f"{self.ingested} new"]
        if self.updated:
            bits.append(f"{self.updated} updated")
        if self.deleted:
            bits.append(f"{self.deleted} deleted")
        if self.skipped_by_policy:
            bits.append(f"{self.skipped_by_policy} skipped")
        if self.used_recovery:
            bits.append("via gap recovery")
        return ", ".join(bits)


# ---------------------------------------------------------------------------
# Identity upsert
# ---------------------------------------------------------------------------

async def upsert_identity(
    session: AsyncSession,
    *,
    address: str,
    display_name: str = "",
    seen_at: Optional[datetime] = None,
    is_me: bool = False,
    bulk: bool = False,
) -> Optional[Identity]:
    """Find or create an identity for an address.

    Atomic upsert rather than get-then-insert: two workers processing messages
    from the same sender concurrently would otherwise both see no row and both
    INSERT, and the second dies on the unique constraint. This is the same
    class of bug the settings service had.
    """
    normalized = normalize_email(address)
    if not normalized or "@" not in normalized:
        return None

    stmt = (
        sqlite_insert(Identity)
        .values(
            id=new_id(),
            kind=IdentityKind.EMAIL.value,
            value_normalized=normalized,
            value_raw=address.strip(),
            display_names=[display_name.strip()] if display_name.strip() else [],
            first_seen=seen_at,
            last_seen=seen_at,
            message_count=1,
            is_role_account=is_role_account(normalized),
            is_bulk_sender=bulk,
            is_me=is_me,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["kind", "value_normalized"])
    )
    await session.execute(stmt)

    identity = (
        await session.execute(
            select(Identity).where(
                Identity.kind == IdentityKind.EMAIL.value,
                Identity.value_normalized == normalized,
            )
        )
    ).scalar_one_or_none()

    if identity is None:
        return None

    # Accumulate what we learned from this sighting.
    if display_name.strip():
        identity.display_names = merge_display_names(
            identity.display_names or [], display_name
        )
    if seen_at:
        if identity.first_seen is None or seen_at < _aware(identity.first_seen):
            identity.first_seen = seen_at
        if identity.last_seen is None or seen_at > _aware(identity.last_seen):
            identity.last_seen = seen_at
    identity.message_count = (identity.message_count or 0) + 1
    if is_me:
        # Derived from the SENT folder — a definitive signal, never unset here.
        identity.is_me = True
    if bulk:
        identity.is_bulk_sender = True

    return identity


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Thread upsert
# ---------------------------------------------------------------------------

async def upsert_thread(
    session: AsyncSession, *, source_id: str, parsed: ParsedMessage
) -> MessageThread:
    stmt = (
        sqlite_insert(MessageThread)
        .values(
            id=new_id(),
            source_id=source_id,
            source_thread_id=parsed.source_thread_id,
            subject_normalized=parsed.subject_normalized[:500],
            message_count=0,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["source_id", "source_thread_id"])
    )
    await session.execute(stmt)

    thread = (
        await session.execute(
            select(MessageThread).where(
                MessageThread.source_id == source_id,
                MessageThread.source_thread_id == parsed.source_thread_id,
            )
        )
    ).scalar_one()

    received = parsed.received_at
    is_from_me = parsed.is_sent

    if thread.first_message_at is None or received < _aware(thread.first_message_at):
        thread.first_message_at = received
    if thread.last_message_at is None or received > _aware(thread.last_message_at):
        thread.last_message_at = received
        if not thread.subject_normalized:
            thread.subject_normalized = parsed.subject_normalized[:500]

    # Denormalised so "who haven't I replied to" is an indexed scan rather than
    # a correlated subquery per thread.
    if is_from_me:
        if thread.last_outbound_at is None or received > _aware(thread.last_outbound_at):
            thread.last_outbound_at = received
    else:
        if thread.last_inbound_at is None or received > _aware(thread.last_inbound_at):
            thread.last_inbound_at = received

    thread.message_count = (thread.message_count or 0) + 1
    if not parsed.is_read:
        thread.unread_count = (thread.unread_count or 0) + 1

    return thread


# ---------------------------------------------------------------------------
# Message ingest
# ---------------------------------------------------------------------------

async def ingest_message(
    session: AsyncSession,
    *,
    source_id: str,
    raw: Dict[str, Any],
    policy: LabelPolicy,
    my_addresses: Set[str],
) -> Optional[Message]:
    """Parse and persist one message. Returns None when policy skips it."""
    parsed = parse_message(raw)

    if parsed.is_draft:
        return None  # never sent; reporting it would be a fabrication
    if not policy.should_ingest(parsed.labels):
        return None

    existing = (
        await session.execute(
            select(Message).where(
                Message.source_id == source_id,
                Message.source_message_id == parsed.source_message_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Body is immutable; labels and read state are not.
        existing.labels = parsed.labels
        existing.is_read = parsed.is_read
        return existing

    bulk = is_bulk_sender(parsed.headers)
    is_from_me = parsed.is_sent or normalize_email(parsed.from_address) in my_addresses

    sender = await upsert_identity(
        session,
        address=parsed.from_address,
        display_name=parsed.from_name,
        seen_at=parsed.received_at,
        is_me=is_from_me,
        bulk=bulk,
    )

    recipient_ids: Dict[str, List[str]] = {"to": [], "cc": [], "bcc": []}
    for field_name, pairs in (
        ("to", parsed.to),
        ("cc", parsed.cc),
        ("bcc", parsed.bcc),
    ):
        for name, address in pairs:
            identity = await upsert_identity(
                session,
                address=address,
                display_name=name,
                seen_at=parsed.received_at,
                is_me=normalize_email(address) in my_addresses,
            )
            if identity is not None:
                recipient_ids[field_name].append(identity.id)

    thread = await upsert_thread(session, source_id=source_id, parsed=parsed)

    fields = dict(
        rfc_message_id=parsed.rfc_message_id or None,
        thread_id=thread.id,
        in_reply_to=parsed.in_reply_to or None,
        references_ids=parsed.references or None,
        from_identity_id=sender.id if sender else None,
        to_ids=recipient_ids["to"],
        cc_ids=recipient_ids["cc"],
        bcc_ids=recipient_ids["bcc"],
        subject=parsed.subject[:1000] or None,
        subject_normalized=parsed.subject_normalized[:1000] or None,
        body_text=parsed.body_text,
        body_html_sanitized=parsed.body_html or None,
        body_clean=parsed.body_clean,
        signature_block=parsed.signature_block or None,
        snippet=parsed.snippet,
        sent_at=parsed.sent_at,
        received_at=parsed.received_at,
        labels=parsed.labels,
        is_read=parsed.is_read,
        is_from_me=is_from_me,
        has_attachments=bool(parsed.attachments),
        size_bytes=parsed.size_bytes,
        truncated=parsed.truncated,
        decode_degraded=parsed.decode_degraded or parsed.date_header_distrusted,
        content_hash=parsed.content_hash,
        list_id=parsed.list_id[:300] or None,
    )

    # Atomic insert-or-skip, not a bare add(): the select above covers the
    # ordinary case, but a manual "check now" and the worker's own poll tick
    # can both be mid-sync for the same source at once, each on its own
    # session — both would see no existing row for a brand-new message, and
    # a plain add() would have the second commit die on the
    # (source_id, source_message_id) unique constraint the module docstring
    # promises is impossible. `on_conflict_do_nothing` rather than do_update:
    # a conflict here means another writer just inserted the same immutable
    # Gmail message a moment ago, not a genuine edit to reconcile.
    await session.execute(
        sqlite_insert(Message)
        .values(source_id=source_id, source_message_id=parsed.source_message_id, **fields)
        .on_conflict_do_nothing(index_elements=["source_id", "source_message_id"])
    )
    # Re-fetch regardless of which writer's insert actually landed — either
    # way the row now exists, and the caller only needs a real Message back.
    return (
        await session.execute(
            select(Message).where(
                Message.source_id == source_id,
                Message.source_message_id == parsed.source_message_id,
            )
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# The sync itself
# ---------------------------------------------------------------------------

async def establish_watermark(
    session: AsyncSession, client: GmailClient, *, source_id: str
) -> str:
    """Record where recording begins. This is the data horizon."""
    profile = await client.get_profile()
    history_id = str(profile.get("historyId") or "")
    if not history_id:
        raise GmailError("Gmail profile returned no historyId; cannot start syncing.")

    now = utcnow()
    stmt = (
        sqlite_insert(SyncState)
        .values(
            source_id=source_id,
            watermark={"history_id": history_id},
            recording_since=now,
            last_success_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["source_id"],
            set_={"watermark": {"history_id": history_id}, "updated_at": now},
        )
    )
    await session.execute(stmt)
    return history_id


async def collect_message_ids(
    client: GmailClient, records: Sequence[Dict[str, Any]]
) -> tuple[Set[str], Set[str]]:
    """Split history records into (added or changed, deleted)."""
    changed: Set[str] = set()
    deleted: Set[str] = set()

    for record in records:
        for added in record.get("messagesAdded", []) or []:
            mid = (added.get("message") or {}).get("id")
            if mid:
                changed.add(mid)
        for removed in record.get("messagesDeleted", []) or []:
            mid = (removed.get("message") or {}).get("id")
            if mid:
                deleted.add(mid)
        # Label changes are metadata updates, not new messages.
        for key in ("labelsAdded", "labelsRemoved"):
            for entry in record.get(key, []) or []:
                mid = (entry.get("message") or {}).get("id")
                if mid:
                    changed.add(mid)

    return changed - deleted, deleted


async def bounded_catch_up(
    client: GmailClient, *, since: datetime, policy: LabelPolicy, max_messages: int
) -> tuple[List[str], Optional[str]]:
    """Date-bounded message list, used for gap recovery and the optional seed.

    One primitive with different bounds, rather than three near-identical code
    paths for seeding, history-expiry recovery, and label-scope changes.
    """
    query = policy.gmail_query()
    date_filter = f"after:{since.strftime('%Y/%m/%d')}"
    full_query = f"{query} {date_filter}".strip()

    ids: List[str] = []
    page_token: Optional[str] = None
    while len(ids) < max_messages:
        payload = await client.list_messages(
            query=full_query, page_token=page_token, max_results=100
        )
        for entry in payload.get("messages", []) or []:
            if entry.get("id"):
                ids.append(entry["id"])
            if len(ids) >= max_messages:
                break
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    profile = await client.get_profile()
    return ids[:max_messages], str(profile.get("historyId") or "") or None


async def sync_once(
    session: AsyncSession,
    client: GmailClient,
    *,
    source_id: str,
    policy: LabelPolicy,
    my_addresses: Set[str],
    mirror_deletions: bool = True,
    max_recovery_messages: int = 2000,
) -> SyncOutcome:
    """One incremental pass. Safe to call repeatedly; safe to crash inside."""
    outcome = SyncOutcome()

    state = await session.get(SyncState, source_id)
    if state is None or not (state.watermark or {}).get("history_id"):
        # Never initialised. Establishing a watermark is the connect path, not
        # a sync — passing 0 to history.list is invalid and returns 404.
        outcome.watermark = await establish_watermark(
            session, client, source_id=source_id
        )
        outcome.complete = True
        return outcome

    start_history_id = state.watermark["history_id"]
    message_ids: Set[str] = set()
    deleted_ids: Set[str] = set()
    new_watermark: Optional[str] = None

    try:
        records, new_watermark = await collect_history(client, start_history_id)
        message_ids, deleted_ids = await collect_message_ids(client, records)
    except HistoryExpired:
        # NOT a full sync. Bridge only the window we were away for.
        outcome.used_recovery = True
        since = _aware(state.last_success_at or state.recording_since or utcnow())
        ids, new_watermark = await bounded_catch_up(
            client,
            since=since - timedelta(days=1),
            policy=policy,
            max_messages=max_recovery_messages,
        )
        message_ids = set(ids)
        log.warning(
            "Gmail history expired; recovered %d messages since %s",
            len(message_ids),
            since.date(),
        )

    for message_id in sorted(message_ids):
        try:
            raw = await client.get_message(message_id)
        except GmailAuthError:
            raise
        except GmailError as exc:
            # One unreadable message must not cost the whole batch.
            outcome.errors.append(f"{message_id}: {exc}")
            continue

        try:
            stored = await ingest_message(
                session,
                source_id=source_id,
                raw=raw,
                policy=policy,
                my_addresses=my_addresses,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to ingest %s", message_id)
            outcome.errors.append(f"{message_id}: {type(exc).__name__}: {exc}")
            continue

        if stored is None:
            outcome.skipped_by_policy += 1
        else:
            outcome.ingested += 1

    if mirror_deletions and deleted_ids:
        outcome.deleted = await soft_delete_messages(
            session, source_id=source_id, source_message_ids=deleted_ids
        )

    # Only now, with every page walked and every message persisted, is it safe
    # to move the watermark. A crash before this point re-reads the same window,
    # which is harmless because ingest is idempotent.
    if new_watermark:
        state.watermark = {"history_id": new_watermark}
        outcome.watermark = new_watermark

    state.last_attempt_at = utcnow()
    state.last_success_at = utcnow()
    state.last_error = "; ".join(outcome.errors[:3]) or None
    state.consecutive_failures = 0
    state.messages_ingested = (state.messages_ingested or 0) + outcome.ingested
    state.messages_skipped = (state.messages_skipped or 0) + outcome.skipped_by_policy
    outcome.complete = True

    return outcome


async def soft_delete_messages(
    session: AsyncSession, *, source_id: str, source_message_ids: Set[str]
) -> int:
    """Mirror an upstream deletion.

    Soft first so retrieval stops seeing it immediately; the hard delete and
    its cascade to attachments on disk runs on a schedule. Deleting the row
    while a downloaded file survives would leave the contents of "deleted" mail
    sitting in the data directory.
    """
    if not source_message_ids:
        return 0
    rows = (
        await session.execute(
            select(Message).where(
                Message.source_id == source_id,
                Message.source_message_id.in_(source_message_ids),
                Message.deleted_at.is_(None),
            )
        )
    ).scalars().all()

    now = utcnow()
    for row in rows:
        row.deleted_at = now
    return len(rows)


async def record_failure(
    session: AsyncSession, *, source_id: str, error: str
) -> None:
    state = await session.get(SyncState, source_id)
    if state is None:
        return
    state.last_attempt_at = utcnow()
    state.last_error = error[:1000]
    state.consecutive_failures = (state.consecutive_failures or 0) + 1
