"""iMessage ingestion and session grouping."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.imessage.reader import (
    DEFAULT_DB_PATH,
    MIN_TEXT_COVERAGE,
    IMessageUnavailable,
    RawIMessage,
    SchemaReport,
    inspect_schema,
    max_rowid,
    open_readonly_copy,
    read_messages,
    text_coverage,
)
from backend.database.models import (
    Chat,
    ChatMessage,
    ChatSession,
    Identity,
    Reaction,
    SyncState,
    new_id,
    utcnow,
)
from backend.pipeline.identity import IdentityKind, normalize_handle

log = logging.getLogger(__name__)


@dataclass
class IMessageSyncOutcome:
    ingested: int = 0
    reactions: int = 0
    sessions: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    watermark: Optional[int] = None
    text_coverage: float = 1.0
    coverage_warning: Optional[str] = None

    def summary(self) -> str:
        bits = [f"{self.ingested} messages"]
        if self.sessions:
            bits.append(f"{self.sessions} sessions")
        if self.reactions:
            bits.append(f"{self.reactions} reactions")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        return ", ".join(bits)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def group_into_sessions(
    messages: Sequence[RawIMessage], *, gap_minutes: int = 30
) -> List[List[RawIMessage]]:
    """Split a chat's messages into conversation bursts.

    The unit of meaning in a text conversation is the burst, not the message:
    "friday works" is meaningless alone and clear alongside the three messages
    before it. Grouping also turns 500 messages a day into roughly 20 units of
    LLM work.

    Input is assumed to be one chat, sorted by time — ROWID order is not time
    order, because iCloud delivers older messages with newer row IDs.
    """
    if not messages:
        return []

    gap = timedelta(minutes=gap_minutes)
    sessions: List[List[RawIMessage]] = []
    current: List[RawIMessage] = []
    previous: Optional[datetime] = None

    for message in messages:
        if message.sent_at is None:
            continue
        if previous is not None and (message.sent_at - previous) > gap:
            if current:
                sessions.append(current)
            current = []
        current.append(message)
        previous = message.sent_at

    if current:
        sessions.append(current)
    return sessions


def render_transcript(messages: Sequence[RawIMessage], *, me_label: str = "Me") -> str:
    """A session as readable text — what gets summarised and embedded."""
    lines = []
    for message in messages:
        if not message.text:
            continue
        who = me_label if message.is_from_me else (message.handle or "Them")
        stamp = message.sent_at.strftime("%H:%M") if message.sent_at else "??:??"
        lines.append(f"[{stamp}] {who}: {message.text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def upsert_handle_identity(
    session: AsyncSession, handle: str, *, seen_at: Optional[datetime], is_me: bool = False
) -> Optional[Identity]:
    """iMessage handles are either an email address or a phone number."""
    normalized = normalize_handle(handle)
    if not normalized:
        return None

    kind = IdentityKind.EMAIL.value if "@" in normalized else IdentityKind.PHONE.value
    await session.execute(
        sqlite_insert(Identity)
        .values(
            id=new_id(),
            kind=kind,
            value_normalized=normalized,
            value_raw=handle,
            display_names=[],
            first_seen=seen_at,
            last_seen=seen_at,
            message_count=0,
            is_me=is_me,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["kind", "value_normalized"])
    )

    identity = (
        await session.execute(
            select(Identity).where(
                Identity.kind == kind, Identity.value_normalized == normalized
            )
        )
    ).scalar_one_or_none()

    if identity is not None:
        identity.message_count = (identity.message_count or 0) + 1
        if seen_at:
            if identity.last_seen is None or seen_at > _aware(identity.last_seen):
                identity.last_seen = seen_at
            if identity.first_seen is None or seen_at < _aware(identity.first_seen):
                identity.first_seen = seen_at
        if is_me:
            identity.is_me = True
    return identity


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def upsert_chat(
    session: AsyncSession, *, source_id: str, raw: RawIMessage
) -> Chat:
    """Get or create the chat row.

    Deliberately does not touch `message_count` or the last-activity
    timestamps — this runs before the message insert it is a foreign key
    for, so at this point it is not yet known whether this call is about to
    store a genuinely new message. See `_record_chat_activity`, called only
    after that insert actually lands a row.
    """
    await session.execute(
        sqlite_insert(Chat)
        .values(
            id=new_id(),
            source_id=source_id,
            source_chat_id=raw.chat_id,
            display_name=raw.chat_name or raw.chat_id,
            is_group=raw.is_group,
            service=raw.service,
            message_count=0,
        )
        .on_conflict_do_nothing(index_elements=["source_id", "source_chat_id"])
    )
    return (
        await session.execute(
            select(Chat).where(
                Chat.source_id == source_id, Chat.source_chat_id == raw.chat_id
            )
        )
    ).scalar_one()


def _record_chat_activity(chat: Chat, raw: RawIMessage) -> None:
    """Bookkeeping for a message that was actually stored.

    Split out from `upsert_chat` and called only after a successful insert:
    two racing syncs can both call `upsert_chat` for the same new message
    (its id is needed before either knows who will win), and bumping the
    count there unconditionally would inflate `message_count` by one for
    every loser of a race that otherwise produces exactly one stored row.
    """
    if raw.sent_at:
        if chat.last_message_at is None or raw.sent_at > _aware(chat.last_message_at):
            chat.last_message_at = raw.sent_at
        if raw.is_from_me:
            if chat.last_outbound_at is None or raw.sent_at > _aware(chat.last_outbound_at):
                chat.last_outbound_at = raw.sent_at
        else:
            if chat.last_inbound_at is None or raw.sent_at > _aware(chat.last_inbound_at):
                chat.last_inbound_at = raw.sent_at
    chat.message_count = (chat.message_count or 0) + 1


async def ingest_batch(
    session: AsyncSession,
    *,
    source_id: str,
    messages: Sequence[RawIMessage],
    gap_minutes: int = 30,
) -> IMessageSyncOutcome:
    """Persist a batch, routing reactions away from the message table."""
    outcome = IMessageSyncOutcome()
    if not messages:
        return outcome

    outcome.text_coverage = text_coverage(list(messages))
    if outcome.text_coverage < MIN_TEXT_COVERAGE:
        # Loud, because the alternative is a connector that reports thousands
        # of rows ingested and every one of them blank.
        outcome.coverage_warning = (
            f"Only {outcome.text_coverage:.0%} of messages yielded any text. This "
            "usually means attributedBody is not being decoded — expected on "
            "macOS Ventura and later. Ingestion continues, but search will be "
            "close to useless until this is fixed."
        )
        log.error(outcome.coverage_warning)

    by_chat: Dict[str, List[RawIMessage]] = {}

    for raw in messages:
        if raw.sent_at is None:
            outcome.skipped += 1
            continue

        # Tapbacks are not messages. Ingesting them fills the corpus with
        # 'Liked "sounds good"' and inflates every count.
        if raw.is_reaction:
            sender = await upsert_handle_identity(
                session, raw.handle, seen_at=raw.sent_at, is_me=raw.is_from_me
            )
            await session.execute(
                sqlite_insert(Reaction)
                .values(
                    id=new_id(),
                    source_id=source_id,
                    source_rowid=raw.rowid,
                    target_guid=raw.reaction_target,
                    from_identity_id=sender.id if sender else None,
                    kind=raw.reaction_kind or "like",
                    removed=raw.reaction_removed,
                    created_at=raw.sent_at,
                )
                .on_conflict_do_nothing(index_elements=["source_id", "source_rowid"])
            )
            outcome.reactions += 1
            continue

        existing = (
            await session.execute(
                select(ChatMessage).where(
                    ChatMessage.source_id == source_id,
                    ChatMessage.source_rowid == raw.rowid,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # A ROWID here has already been ingested and is behind the
            # watermark, so under normal single-threaded polling this branch
            # is nearly unreachable — `read_messages` only ever returns rows
            # newer than the watermark. It exists for the case where it *is*
            # reachable: a manual "check now" overlapping the worker's own
            # tick, both reading the same not-yet-advanced watermark and
            # observing the same "new" row.
            existing.text = raw.text
            existing.is_edited = raw.is_edited
            if raw.is_unsent and existing.deleted_at is None:
                existing.deleted_at = utcnow()
            outcome.skipped += 1
            continue

        chat = await upsert_chat(session, source_id=source_id, raw=raw)
        sender = await upsert_handle_identity(
            session, raw.handle, seen_at=raw.sent_at, is_me=raw.is_from_me
        )

        # Atomic insert-or-skip, not a bare add(): the select above already
        # covers the ordinary case, but two concurrent syncs can both pass it
        # (both see no row, both proceed to insert) — proved by racing two
        # sessions through this exact path. `on_conflict_do_nothing` rather
        # than do_update because a conflict here means another writer just
        # inserted the same observation of the same source row a moment ago,
        # not a genuine edit to reconcile.
        result = await session.execute(
            sqlite_insert(ChatMessage)
            .values(
                id=new_id(),
                source_id=source_id,
                source_rowid=raw.rowid,
                guid=raw.guid,
                chat_id=chat.id,
                from_identity_id=sender.id if sender else None,
                text=raw.text,
                text_source=raw.text_source,
                sent_at=raw.sent_at,
                is_from_me=raw.is_from_me,
                service=raw.service,
                is_edited=raw.is_edited,
                is_unsent=raw.is_unsent,
                has_attachments=raw.has_attachments,
                deleted_at=utcnow() if raw.is_unsent else None,
            )
            .on_conflict_do_nothing(index_elements=["source_id", "source_rowid"])
        )
        if result.rowcount == 0:
            # Lost the race: someone else's copy of this row is already
            # there. Do not double-count it, do not double-bump the chat's
            # activity fields, and do not feed it into session-building again.
            outcome.skipped += 1
            continue

        _record_chat_activity(chat, raw)
        outcome.ingested += 1
        by_chat.setdefault(raw.chat_id, []).append(raw)

    # Sessions are built per chat, in time order.
    for chat_key, chat_messages in by_chat.items():
        chat = (
            await session.execute(
                select(Chat).where(
                    Chat.source_id == source_id, Chat.source_chat_id == chat_key
                )
            )
        ).scalar_one_or_none()
        if chat is None:
            continue

        ordered = sorted(chat_messages, key=lambda m: m.sent_at or utcnow())
        groups = group_into_sessions(ordered, gap_minutes=gap_minutes)
        outcome.sessions += await _continue_or_start_sessions(
            session, chat=chat, groups=groups, gap_minutes=gap_minutes
        )

    outcome.watermark = max(m.rowid for m in messages)
    return outcome


#: A session that has already absorbed this many messages stops extending
#: across ticks even when the gap is small — otherwise a chat that never goes
#: quiet for `gap_minutes` would grow one row's transcript without bound,
#: which would eventually be too large to summarise or embed as one unit.
MAX_SESSION_MESSAGES = 300


async def _continue_or_start_sessions(
    session: AsyncSession,
    *,
    chat: Chat,
    groups: Sequence[Sequence[RawIMessage]],
    gap_minutes: int,
) -> int:
    """Turn message groups into `ChatSession` rows, continuing the chat's
    most recent session across ingestion batches when the real-world gap
    between them is small. Returns the number of *new* session rows created.

    Sessions are built per poll tick, but a tick boundary is not a
    conversational one: the default poll interval (30s) is far shorter than
    the session gap (30 minutes by default), so any live back-and-forth
    spanning more than one tick — which is most conversations, not an edge
    case — would otherwise fragment into several artificially small sessions
    purely because of when the connector happened to look, not because the
    conversation actually paused. Confirmed by ingesting the same
    conversation across two batches 30 seconds apart: without this, it
    produced two session rows instead of one.
    """
    created = 0
    if not groups:
        return created

    gap = timedelta(minutes=gap_minutes)
    remaining = list(groups)
    first_group = remaining[0]
    first_sent = first_group[0].sent_at

    if first_sent is not None:
        open_session = (
            await session.execute(
                select(ChatSession)
                .where(ChatSession.chat_id == chat.id)
                .order_by(ChatSession.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if (
            open_session is not None
            and open_session.message_count < MAX_SESSION_MESSAGES
            # Absolute, not signed: iCloud sync can deliver an older message
            # after a newer one, so "the new group's first message" is not
            # guaranteed to be later than the existing session's last one.
            # A signed check would treat a message from hours earlier as
            # "close" (a large negative gap is still <= a positive threshold)
            # and wrongly extend into it.
            and abs(first_sent - _aware(open_session.ended_at)) <= gap
        ):
            extension = render_transcript(first_group)
            if extension:
                open_session.transcript = f"{open_session.transcript}\n{extension}"
                open_session.started_at = min(_aware(open_session.started_at), first_sent)
                open_session.ended_at = max(
                    _aware(open_session.ended_at), first_group[-1].sent_at  # type: ignore[type-var]
                )
                open_session.message_count += len(first_group)
                # Stale relative to the now-longer transcript — a future
                # summarising pass must not treat it as still accurate.
                open_session.summary = None
            remaining = remaining[1:]

    for group in remaining:
        transcript = render_transcript(group)
        if not transcript:
            continue
        session.add(
            ChatSession(
                id=new_id(),
                chat_id=chat.id,
                started_at=group[0].sent_at,  # type: ignore[arg-type]
                ended_at=group[-1].sent_at,  # type: ignore[arg-type]
                message_count=len(group),
                transcript=transcript,
            )
        )
        created += 1
    return created


def _read_from_disk(
    db_path: Path, *, after_rowid: int, batch_size: int, need_batch: bool
) -> Tuple[SchemaReport, int, List[RawIMessage]]:
    """Every blocking step, isolated so it can run off the event loop.

    `open_readonly_copy` does a `shutil.copy2` of the whole database — on a
    long message history that can be hundreds of megabytes to a few
    gigabytes. Run synchronously on the event loop, that copy would stall
    every other request the server is handling for as long as it takes,
    every single poll tick, forever. It belongs in a thread.

    `need_batch` skips the row read on a first run, matching the original
    behaviour: a bootstrap only wants `MAX(ROWID)`, and materialising
    thousands of `RawIMessage` objects just to discard them would be pure
    waste.
    """
    conn, _copy = open_readonly_copy(db_path)
    try:
        report = inspect_schema(conn)
        if not report.ok:
            return report, 0, []
        top = max_rowid(conn)
        batch = (
            list(read_messages(conn, after_rowid=after_rowid, limit=batch_size))
            if need_batch
            else []
        )
        return report, top, batch
    finally:
        conn.close()


async def sync_imessage_once(
    session: AsyncSession,
    *,
    source_id: str,
    db_path: Optional[str] = None,
    gap_minutes: int = 30,
    batch_size: int = 2000,
) -> IMessageSyncOutcome:
    """One pass over the local database."""
    state = await session.get(SyncState, source_id)
    after = int((state.watermark or {}).get("rowid", 0)) if state else 0
    first_run = state is None or not (state.watermark or {}).get("rowid")
    path = Path(db_path) if db_path else DEFAULT_DB_PATH

    report, top, batch = await asyncio.to_thread(
        _read_from_disk,
        path,
        after_rowid=after,
        batch_size=batch_size,
        need_batch=not first_run,
    )
    if not report.ok:
        raise IMessageUnavailable(report.message)

    if first_run:
        # Live-only: start from the newest row and record nothing behind it.
        now = utcnow()
        if state is None:
            await session.execute(
                sqlite_insert(SyncState).values(
                    source_id=source_id,
                    watermark={"rowid": top},
                    recording_since=now,
                    last_attempt_at=now,
                    last_success_at=now,
                    updated_at=now,
                )
            )
        else:
            state.watermark = {"rowid": top}
            state.recording_since = state.recording_since or now
            state.last_success_at = now
        return IMessageSyncOutcome(watermark=top)

    outcome = await ingest_batch(
        session, source_id=source_id, messages=batch, gap_minutes=gap_minutes
    )

    if state is not None:
        if outcome.watermark:
            state.watermark = {"rowid": outcome.watermark}
        state.last_attempt_at = utcnow()
        state.last_success_at = utcnow()
        state.last_error = outcome.coverage_warning or (
            "; ".join(outcome.errors[:3]) or None
        )
        state.consecutive_failures = 0
        state.messages_ingested = (state.messages_ingested or 0) + outcome.ingested

    return outcome
