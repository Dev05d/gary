"""SQLAlchemy models.

Milestone 1 scope only: conversations, chat turns, connector registry, and a
key/value settings table.  The message / thread / contact / calendar / embedding
tables from spec §5 land in M2–M3 alongside the code that populates them —
see docs/ARCHITECTURE.md for the full target schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {Dict[str, Any]: JSON, List[str]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300), default="New conversation")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    messages: Mapped[List["ChatTurn"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ChatTurn.created_at",
        lazy="selectin",
    )

    __table_args__ = (Index("ix_conversations_updated_at", "updated_at"),)


class ChatTurn(Base):
    """One message in the agent chat UI (not an ingested email — that's `messages`)."""

    __tablename__ = "chat_turns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user|assistant|system
    content: Mapped[str] = mapped_column(Text, default="")

    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Populated from M5 onward; shape is fixed now so the UI contract is stable.
    tool_calls: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    citations: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_chat_turns_conversation_created", "conversation_id", "created_at"),
    )


class Source(Base, TimestampMixin):
    """A configured connector instance (one Gmail account, one calendar, ...)."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)  # gmail|gcal|imessage|...
    display_name: Mapped[str] = mapped_column(String(200), default="")
    account_identifier: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="disconnected")
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Incremental-sync bookmark (Gmail historyId, Calendar syncToken, iMessage ROWID).
    sync_cursor: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    config: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("kind", "account_identifier", name="uq_source_kind_account"),
        Index("ix_sources_kind", "kind"),
    )


class AppSetting(Base):
    """Runtime-editable settings (notification thresholds, UI prefs).

    Distinct from `.env`, which holds boot-time infrastructure config.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# ===========================================================================
# Facts plane (Milestone 2)
#
# Structured, exact, indexed. See docs/DATA-MODEL.md.
#
# The layering rule that governs everything below: messages foreign-key to an
# *identity* (a raw handle), never to a *person* (an opinion about handles).
# Identity resolution improves over time, and improving it must never require
# rewriting the message table.
# ===========================================================================


class Identity(Base, TimestampMixin):
    """A raw handle: an email address, a phone number, an Apple ID.

    Never merged, never deleted. Two identities belonging to one human are
    linked through `identity_links`, which is reversible.
    """

    __tablename__ = "identities"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    value_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    value_raw: Mapped[str] = mapped_column(String(320), default="")

    #: Every display-name variant seen. One of them may be the only bridge to
    #: another identity, so none is ever overwritten.
    display_names: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    first_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    is_role_account: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bulk_sender: Mapped[bool] = mapped_column(Boolean, default=False)
    #: One of the user's own addresses. Derived from the SENT folder, because a
    #: missed alias makes "who haven't I replied to" wrong from the first message.
    is_me: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("kind", "value_normalized", name="uq_identity_value"),
        Index("ix_identities_is_me", "is_me"),
        Index("ix_identities_role", "is_role_account"),
    )


class Person(Base, TimestampMixin):
    """A resolved human — a view over identities, not a replacement for them."""

    __tablename__ = "persons"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    canonical_name: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_me: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(20), default="auto")  # auto|user


class IdentityLink(Base):
    """Claim that an identity belongs to a person, with its evidence.

    Withdrawal sets `unlinked_at` rather than deleting the row: a bad merge
    must be undoable without reconstructing why it was made.
    """

    __tablename__ = "identity_links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    identity_id: Mapped[str] = mapped_column(
        ForeignKey("identities.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[str] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    signal: Mapped[str] = mapped_column(String(40), default="")
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    unlinked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_links_identity", "identity_id"),
        Index("ix_links_person", "person_id"),
    )


class MessageThread(Base, TimestampMixin):
    """A conversation.

    `last_inbound_at` / `last_outbound_at` are denormalised on purpose: they
    turn "who haven't I replied to" from a correlated subquery per thread into
    one indexed scan.
    """

    __tablename__ = "message_threads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    source_thread_id: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_normalized: Mapped[str] = mapped_column(String(500), default="")
    participant_ids: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    first_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    message_count: Mapped[int] = mapped_column(Integer, default=0)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_thread_id", name="uq_thread_source"),
        Index("ix_threads_last_message", "last_message_at"),
        Index("ix_threads_awaiting_reply", "last_inbound_at", "last_outbound_at"),
    )


class Message(Base):
    """One ingested message.

    `(source_id, source_message_id)` is unique, which is what makes ingestion
    idempotent: replaying a sync is a no-op, so crash recovery is boring.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    source_message_id: Mapped[str] = mapped_column(String(120), nullable=False)
    #: RFC 5322 Message-ID — used to recognise the same mail across accounts.
    rfc_message_id: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)

    thread_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("message_threads.id", ondelete="SET NULL"), nullable=True
    )
    in_reply_to: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    references_ids: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    from_identity_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    to_ids: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    cc_ids: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    bcc_ids: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    subject: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    subject_normalized: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html_sanitized: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Quotes and signature removed. This is what gets embedded and extracted from.
    body_clean: Mapped[str] = mapped_column(Text, default="")
    #: Kept separately — the richest source of identity-linking evidence.
    signature_block: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    snippet: Mapped[str] = mapped_column(String(500), default="")

    #: Date header. Sender-written, spoofable, frequently wrong.
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Server receive time (Gmail internalDate). THE ordering key.
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Provenance only. Never used for ordering — a clock correction reorders it.
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    labels: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=True)
    is_from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    decode_degraded: Mapped[bool] = mapped_column(Boolean, default=False)

    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    list_id: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("source_id", "source_message_id", name="uq_message_source"),
        Index("ix_messages_received", "received_at"),
        Index("ix_messages_sender_received", "from_identity_id", "received_at"),
        Index("ix_messages_thread_received", "thread_id", "received_at"),
        Index("ix_messages_rfc_id", "rfc_message_id"),
        Index("ix_messages_deleted", "deleted_at"),
    )


class Attachment(Base):
    """File attached to a message.

    Stored under a generated ID, never the sender-supplied filename — a name
    like '../../.ssh/authorized_keys' must never become a path. Content-hash
    addressed, so a file forwarded repeatedly costs one copy and deletion is
    reference-counted.
    """

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    source_attachment_id: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)

    filename: Mapped[str] = mapped_column(String(500), default="")
    mime_type: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    local_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    #: pending | downloaded | skipped_too_large | skipped_budget | failed
    download_status: Mapped[str] = mapped_column(String(30), default="pending")
    #: pending | extracted | empty | unreadable | ocr | not_applicable
    extract_status: Mapped[str] = mapped_column(String(30), default="pending")
    extracted_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_attachments_message", "message_id"),
        Index("ix_attachments_hash", "content_hash"),
    )


class SyncState(Base):
    """Per-source ingestion bookmark and health.

    `recording_since` is the data horizon: the moment this source started
    producing data. Every temporal answer is bounded by it, because with
    live-only ingestion an empty result is otherwise indistinguishable from
    "it never happened".
    """

    __tablename__ = "sync_state"

    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    #: Opaque per-source token: Gmail historyId, Calendar syncToken, iMessage
    #: ROWID. Never computed with — history IDs are non-contiguous.
    watermark: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    recording_since: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    messages_ingested: Mapped[int] = mapped_column(Integer, default=0)
    messages_skipped: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class OAuthCredential(Base):
    """Encrypted OAuth tokens. Never leaves the backend."""

    __tablename__ = "oauth_credentials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(40), default="google")
    account_email: Mapped[str] = mapped_column(String(320), default="")

    #: AES-256-GCM, bound to account_email as additional authenticated data so
    #: a blob moved between rows fails to decrypt rather than authorising the
    #: wrong mailbox.
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("provider", "account_email", name="uq_credential_account"),
    )


# ===========================================================================
# Calendar (Milestone 6)
#
# Calendar syncs a *window*, not a watermark. A calendar's value is in the
# future, and tomorrow's meeting was created last week — a "from now on" rule
# would make exactly the events you care about invisible.
# ===========================================================================


class CalendarEvent(Base):
    """One event instance.

    Recurring series are stored as **expanded instances** within the sync
    window rather than as a rule. "What do I have Tuesday?" must be an indexed
    range scan; expanding RRULEs at query time is both slow and subtly wrong
    around exceptions and DST.
    """

    __tablename__ = "calendar_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    source_event_id: Mapped[str] = mapped_column(String(400), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(400), default="primary")

    ical_uid: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    #: Links an instance back to its series.
    recurring_event_id: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    is_instance_exception: Mapped[bool] = mapped_column(Boolean, default=False)

    title: Mapped[str] = mapped_column(String(1000), default="")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: An all-day event on the 15th is the 15th *locally*. Storing it as UTC
    #: midnight puts it on the 14th for anyone west of Greenwich, so the local
    #: date is kept verbatim alongside the instant.
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    local_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    timezone_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    organizer_identity_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    attendees: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    #: accepted | declined | tentative | needsAction. A declined event is not
    #: on your calendar in any sense a briefing should mention.
    my_response: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")

    html_link: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    updated_at_remote: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("source_id", "source_event_id", name="uq_event_source"),
        Index("ix_events_starts", "starts_at"),
        Index("ix_events_range", "starts_at", "ends_at"),
        Index("ix_events_series", "recurring_event_id"),
    )


# ===========================================================================
# iMessage (Milestone 7)
#
# Structurally different enough from email that reusing those tables would
# produce garbage: the unit of meaning is a conversation *session*, not a
# message. "friday works" means nothing alone.
# ===========================================================================


class Chat(Base):
    """An iMessage conversation — 1:1 or group."""

    __tablename__ = "im_chats"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    source_chat_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(300), default="")
    is_group: Mapped[bool] = mapped_column(Boolean, default=False)
    participant_ids: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    service: Mapped[str] = mapped_column(String(20), default="iMessage")  # iMessage|SMS

    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("source_id", "source_chat_id", name="uq_chat_source"),
        Index("ix_chats_last_message", "last_message_at"),
    )


class ChatSession(Base):
    """A burst of messages with no long gap — the unit that gets embedded.

    500 messages a day becomes roughly 20 sessions, which is both a sane
    amount of LLM work and the only granularity at which a short reply carries
    meaning.
    """

    __tablename__ = "im_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("im_chats.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    participant_ids: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    transcript: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_sessions_chat_started", "chat_id", "started_at"),)


class ChatMessage(Base):
    """One iMessage. Full fidelity, even though sessions are what get embedded."""

    __tablename__ = "im_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    #: chat.db ROWID — monotonic, so it is the watermark. Never used for
    #: ordering, because iCloud sync delivers older messages with newer ROWIDs.
    source_rowid: Mapped[int] = mapped_column(Integer, nullable=False)
    guid: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    chat_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("im_chats.id", ondelete="CASCADE"), nullable=True
    )
    session_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("im_sessions.id", ondelete="SET NULL"), nullable=True
    )
    from_identity_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )

    text: Mapped[str] = mapped_column(Text, default="")
    #: text_column | attributed_body | attachment_only | empty. Recorded so
    #: coverage is measurable: on recent macOS the plain column is usually
    #: NULL, and a connector that silently ingested nothing would look healthy.
    text_source: Mapped[str] = mapped_column(String(30), default="text_column")

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    service: Mapped[str] = mapped_column(String(20), default="iMessage")

    is_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    is_unsent: Mapped[bool] = mapped_column(Boolean, default=False)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)

    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("source_id", "source_rowid", name="uq_im_message_rowid"),
        Index("ix_im_messages_sent", "sent_at"),
        Index("ix_im_messages_chat_sent", "chat_id", "sent_at"),
        Index("ix_im_messages_session", "session_id"),
    )


class Reaction(Base):
    """A tapback.

    NOT a message. `associated_message_type` 2000–2005 means a reaction was
    added and 3000–3005 removed; ingesting those as messages fills the corpus
    with 'Liked "sounds good"', poisons embeddings, and inflates every count.
    """

    __tablename__ = "im_reactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    source_rowid: Mapped[int] = mapped_column(Integer, nullable=False)
    target_guid: Mapped[str] = mapped_column(String(200), default="")
    from_identity_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="like")
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source_id", "source_rowid", name="uq_im_reaction_rowid"),
        Index("ix_reactions_target", "target_guid"),
    )
