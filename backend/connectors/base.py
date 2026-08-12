"""Connector interfaces and the normalised schema every source maps into.

No implementations here — those arrive per milestone. This file exists in
Milestone 1 because it is the contract the rest of the system is built against:
the retrieval layer, the agent tools, and the UI all speak `NormalizedMessage`,
not "a Gmail thing".

Four acquisition classes, because the sources genuinely differ (see
docs/ARCHITECTURE.md §1.1):

    PushConnector    webhook / Pub/Sub        Gmail
    PollConnector    incremental sync token   Calendar, Outlook
    LocalConnector   local file or DB tail    iMessage, files
    ImportConnector  one-shot archive         Discord / Instagram exports

They differ only in how bytes are acquired. Normalisation, storage, embedding,
classification, and retrieval are shared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional

from pydantic import BaseModel, Field


class SourceKind(str, Enum):
    GMAIL = "gmail"
    GOOGLE_CALENDAR = "gcal"
    IMESSAGE = "imessage"
    DISCORD_EXPORT = "discord_export"
    INSTAGRAM_EXPORT = "instagram_export"
    LOCAL_FILES = "files"


class Attachment(BaseModel):
    filename: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    source_attachment_id: Optional[str] = None
    local_path: Optional[str] = None


class Participant(BaseModel):
    """A person on a message. `handle` is source-native, `display_name` optional."""

    handle: str  # email address, phone number, discord id, ...
    display_name: Optional[str] = None


class NormalizedMessage(BaseModel):
    """The common shape every message-like source maps into (spec §3).

    `source_message_id` must be stable and unique within a source: it is the
    idempotency key that makes re-running a sync a no-op rather than a
    duplicate (spec §18).
    """

    source: SourceKind
    source_message_id: str
    thread_id: Optional[str] = None

    sender: Participant
    recipients: List[Participant] = Field(default_factory=list)

    subject: Optional[str] = None
    body_text: str = ""
    body_html: Optional[str] = None

    timestamp: datetime
    is_from_me: bool = False
    is_read: bool = True

    attachments: List[Attachment] = Field(default_factory=list)
    labels: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class NormalizedCalendarEvent(BaseModel):
    """Spec §4."""

    source: SourceKind
    source_event_id: str

    title: str
    description: Optional[str] = None
    location: Optional[str] = None

    starts_at: datetime
    ends_at: Optional[datetime] = None
    all_day: bool = False

    attendees: List[Participant] = Field(default_factory=list)
    recurrence: Optional[str] = None
    status: Optional[str] = None
    organizer: Optional[Participant] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SyncResult(BaseModel):
    fetched: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    errors: List[str] = Field(default_factory=list)
    #: Opaque bookmark persisted to `sources.sync_cursor` for the next run.
    cursor: Optional[Dict[str, Any]] = None


class ConnectorStatus(BaseModel):
    connected: bool
    detail: Optional[str] = None
    account: Optional[str] = None


class Connector(ABC):
    """Base for every source.

    Implementations must be **idempotent**: running `sync()` twice over the
    same window must not create duplicates. The unique constraint on
    (source_id, source_message_id) enforces this at the database level, but
    connectors should also avoid the wasted work.
    """

    kind: SourceKind

    def __init__(self, source_id: str, config: Optional[Dict[str, Any]] = None) -> None:
        self.source_id = source_id
        self.config = config or {}

    @abstractmethod
    async def status(self) -> ConnectorStatus:
        """Is this connector usable right now?"""

    @abstractmethod
    async def sync(self, cursor: Optional[Dict[str, Any]] = None) -> SyncResult:
        """Fetch everything new since `cursor` and persist it.

        `cursor is None` means a full backfill.
        """

    async def aclose(self) -> None:
        return None


class PushConnector(Connector):
    """Real-time via a provider webhook (Gmail watch + Pub/Sub).

    `sync()` remains the fallback path — spec §3 requires periodic
    reconciliation because push delivery is best-effort, not guaranteed.
    """

    @abstractmethod
    async def register_push(self) -> Dict[str, Any]:
        """Subscribe to provider notifications. Returns renewal metadata."""

    @abstractmethod
    async def handle_push(self, payload: Dict[str, Any]) -> SyncResult:
        """Process one push notification."""


class PollConnector(Connector):
    """Periodic incremental sync using a provider sync token."""

    #: Minimum seconds between polls. Spec §3: do not poll unnecessarily.
    poll_interval_seconds: int = 300


class LocalConnector(Connector):
    """Reads data already on this machine.

    Implementations must open source databases **read-only** and, where the
    file may be written concurrently by another application, work from an
    immutable snapshot rather than the live file.
    """

    @abstractmethod
    async def check_access(self) -> ConnectorStatus:
        """Verify the OS permission needed (e.g. macOS Full Disk Access)."""


class ImportConnector(Connector):
    """One-shot ingestion of an official data-export archive.

    For services whose APIs do not expose personal message history to the
    account owner (Discord, Instagram). A snapshot, not a live feed — the UI
    must present it as such rather than implying continuous sync.
    """

    @abstractmethod
    async def import_archive(self, archive_path: str) -> AsyncIterator[NormalizedMessage]:
        """Yield normalised messages from an export archive on disk."""
