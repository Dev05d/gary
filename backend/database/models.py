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
