"""Reading the local iMessage database.

Your own messages, on your own Mac, through a read-only copy. No API is
involved and none exists; this is a local file you already own.

Four things here will each silently ruin the corpus if missed, and all four
are handled:

1. **The text is usually not in the `text` column.** On macOS Ventura and later
   — and universally on macOS 26 — `message.text` is NULL and the content lives
   in `attributedBody` as an Apple *typedstream* archive. That is `NSArchiver`
   format, not a modern `NSKeyedArchiver` bplist, so `plistlib` returns junk. A
   naive connector ingests thousands of empty messages and reports success.
2. **Tapbacks are stored as messages.** Ingesting them fills the corpus with
   `Liked "sounds good"`.
3. **Timestamps are Apple epoch** — nanoseconds since 2001-01-01 on modern
   macOS, *seconds* on older versions.
4. **The database is locked.** Messages.app holds it open with WAL companions,
   so it must be copied and opened immutable, never touched in place.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

#: Apple's epoch. Everything in chat.db counts from here.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

#: Values above this are nanoseconds; below, seconds. Modern macOS writes
#: nanoseconds, older versions wrote seconds, and misreading puts every message
#: in 1970 or the far future.
NANOSECOND_THRESHOLD = 1e11

#: Sanity bounds — iMessage launched in 2011, and nothing is from the future.
EARLIEST_PLAUSIBLE = datetime(2010, 1, 1, tzinfo=timezone.utc)

#: associated_message_type ranges. 2000–2005 add a tapback, 3000–3005 remove one.
REACTION_ADDED = range(2000, 2006)
REACTION_REMOVED = range(3000, 3006)

REACTION_NAMES = {
    0: "like", 1: "like", 2: "dislike", 3: "laugh",
    4: "emphasis", 5: "question",
}


class IMessageUnavailable(RuntimeError):
    """The database cannot be read, with a reason the user can act on."""


def apple_time_to_datetime(value: Any) -> Optional[datetime]:
    """Apple epoch → UTC datetime, tolerating both unit conventions."""
    if value in (None, 0):
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None

    seconds = raw / 1e9 if abs(raw) > NANOSECOND_THRESHOLD else raw
    try:
        moment = APPLE_EPOCH + timedelta(seconds=seconds)
    except (OverflowError, OSError):
        return None

    # Reject rather than store an obviously wrong timestamp — a message dated
    # 1970 would sort to the beginning of every query forever.
    if moment < EARLIEST_PLAUSIBLE or moment > datetime.now(timezone.utc) + timedelta(days=2):
        return None
    return moment


#: Apple's placeholder for an inline attachment. A photo-only message has this
#: as its entire text; storing it would fill the corpus with invisible
#: single-character "messages" and embed them.
OBJECT_REPLACEMENT = "￼"

#: Attribute-run keys Messages.app writes alongside the text. They are NSString
#: objects too, so they are indistinguishable from message text by type.
_INTERNAL_PREFIXES = ("__k", "NSAttribute", "NSFont", "NSColor", "NSParagraph")


def extract_attributed_body(blob: Optional[bytes]) -> Optional[str]:
    """Pull the message text out of an `attributedBody` typedstream archive.

    Uses a real typedstream reader. The common shortcut — scanning the blob for
    `NSString` and slicing — breaks on multi-byte characters and on the longer
    length encoding, and fails silently by returning truncated text.

    Three details decide whether this works on real data:

    * The text is the value of a ``+``-encoded field, which the reader yields
      as bare :class:`bytes`. Every other string in the archive (class names,
      type encodings) is consumed internally or wrapped in its own event type,
      so ``+`` is a precise filter rather than a guess.
    * The **first** such value is the message. Taking the longest instead picks
      ``__kIMMessagePartAttributeName`` over a message like "ok".
    * Real blobs carry trailing structures this reader rejects. Parsing is
      therefore incremental, and text already recovered survives a failure
      further down the stream.
    """
    if not blob:
        return None
    if blob[:8] == b"bplist00":
        # A keyed archive, not a typedstream. Nothing here can read it, and
        # guessing at its bytes would produce plausible-looking garbage.
        log.debug("attributedBody is a bplist, not a typedstream; skipping")
        return None

    try:
        from typedstream.stream import TypedStreamReader
    except ImportError:  # pragma: no cover - dependency missing
        log.warning("pytypedstream is not installed; attributedBody cannot be read")
        return None

    strings: List[str] = []
    plain_encoding = False
    try:
        for event in TypedStreamReader.from_data(blob):
            name = type(event).__name__
            if name == "BeginTypedValues":
                # `.encodings` is the list of type encodings for the values
                # that follow; only `+` carries string contents.
                plain_encoding = b"+" in getattr(event, "encodings", [])
                continue
            if plain_encoding and isinstance(event, bytes):
                try:
                    strings.append(event.decode("utf-8"))
                except UnicodeDecodeError:
                    strings.append(event.decode("utf-8", "replace"))
                plain_encoding = False
                # The first string is the message; the rest are attribute keys.
                # Stopping here also avoids parsing the part of the archive
                # most likely to be malformed.
                break
    except Exception:  # noqa: BLE001 - a malformed blob must not kill a batch
        if not strings:
            log.debug("attributedBody could not be parsed", exc_info=True)
            return None

    for candidate in strings:
        if candidate.startswith(_INTERNAL_PREFIXES):
            continue
        cleaned = candidate.replace(OBJECT_REPLACEMENT, "").strip()
        if cleaned:
            return cleaned
    return None


@dataclass
class RawIMessage:
    rowid: int
    guid: str
    text: str
    text_source: str
    sent_at: Optional[datetime]
    is_from_me: bool
    service: str
    handle: str
    chat_id: str
    chat_name: str
    is_group: bool
    has_attachments: bool
    is_edited: bool = False
    is_unsent: bool = False
    #: Set when the row is a tapback rather than a message.
    reaction_kind: Optional[str] = None
    reaction_removed: bool = False
    reaction_target: str = ""

    @property
    def is_reaction(self) -> bool:
        return self.reaction_kind is not None


@dataclass
class SchemaReport:
    ok: bool
    version_hint: str = ""
    missing_columns: List[str] = field(default_factory=list)
    message: str = ""


#: Columns the reader depends on. Names drift between macOS releases, and
#: mis-mapping one silently produces wrong data rather than an error.
REQUIRED_MESSAGE_COLUMNS = {
    "ROWID", "guid", "text", "date", "is_from_me", "service",
    "associated_message_type", "handle_id",
}


def inspect_schema(conn: sqlite3.Connection) -> SchemaReport:
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(message)")}
    except sqlite3.Error as exc:
        return SchemaReport(False, message=f"Could not read the message table: {exc}")

    missing = sorted(REQUIRED_MESSAGE_COLUMNS - cols)
    if missing:
        return SchemaReport(
            False,
            missing_columns=missing,
            message=(
                "This chat.db has an unfamiliar schema (missing: "
                f"{', '.join(missing)}). Refusing to sync rather than "
                "mis-reading columns. This usually means a macOS version newer "
                "than this connector knows about."
            ),
        )

    hint = "ventura+" if "attributedBody" in cols else "legacy"
    return SchemaReport(True, version_hint=hint)


def open_readonly_copy(db_path: Path, workdir: Optional[Path] = None) -> Tuple[sqlite3.Connection, Path]:
    """Copy chat.db and its WAL companions, then open the copy immutably.

    Messages.app holds the live database open. Opening it directly risks lock
    contention and, with a write-capable handle, corruption of the user's real
    message history — an unacceptable failure for a read-only tool.
    """
    if not db_path.exists():
        raise IMessageUnavailable(
            f"No Messages database at {db_path}. This connector only works on macOS."
        )

    try:
        target_dir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="gary-imessage-"))
        target_dir.mkdir(parents=True, exist_ok=True)
        copy_path = target_dir / "chat.db"
        shutil.copy2(db_path, copy_path)
        # -wal and -shm carry recent messages not yet checkpointed into the
        # main file. Copying only chat.db silently loses the newest data.
        for suffix in ("-wal", "-shm"):
            companion = db_path.with_name(db_path.name + suffix)
            if companion.exists():
                shutil.copy2(companion, copy_path.with_name(copy_path.name + suffix))
    except PermissionError as exc:
        raise IMessageUnavailable(
            "Permission denied reading the Messages database. Grant Full Disk "
            "Access: System Settings → Privacy & Security → Full Disk Access → "
            "add the app running Gary (Terminal, or the Gary app), then restart it."
        ) from exc
    except OSError as exc:
        raise IMessageUnavailable(f"Could not copy the Messages database: {exc}") from exc

    conn = sqlite3.connect(f"file:{copy_path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, copy_path


MESSAGE_QUERY = """
SELECT
    m.ROWID                      AS rowid,
    m.guid                       AS guid,
    m.text                       AS text,
    {attributed}                 AS attributed_body,
    m.date                       AS date,
    m.is_from_me                 AS is_from_me,
    m.service                    AS service,
    m.associated_message_type    AS assoc_type,
    m.associated_message_guid    AS assoc_guid,
    m.cache_has_attachments      AS has_attachments,
    {edited}                     AS date_edited,
    {retracted}                  AS date_retracted,
    h.id                         AS handle,
    c.ROWID                      AS chat_rowid,
    c.chat_identifier            AS chat_identifier,
    c.display_name               AS chat_display_name,
    c.style                      AS chat_style
FROM message m
LEFT JOIN handle h            ON m.handle_id = h.ROWID
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c              ON c.ROWID = cmj.chat_id
WHERE m.ROWID > ?
ORDER BY m.ROWID ASC
LIMIT ?
"""


def _column_or_null(cols: set, name: str) -> str:
    return f"m.{name}" if name in cols else "NULL"


def read_messages(
    conn: sqlite3.Connection, *, after_rowid: int = 0, limit: int = 2000
) -> Iterator[RawIMessage]:
    """Yield messages newer than a ROWID watermark.

    ROWID is the watermark because it is monotonic. It is *not* the sort order:
    iCloud delivers messages from other devices with older timestamps and newer
    ROWIDs, so ordering and querying use `sent_at`.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(message)")}
    query = MESSAGE_QUERY.format(
        attributed=_column_or_null(cols, "attributedBody"),
        edited=_column_or_null(cols, "date_edited"),
        retracted=_column_or_null(cols, "date_retracted"),
    )

    for row in conn.execute(query, (after_rowid, limit)):
        parsed = _row_to_message(row)
        if parsed is not None:
            yield parsed


def _row_to_message(row: sqlite3.Row) -> Optional[RawIMessage]:
    assoc_type = row["assoc_type"] or 0

    reaction_kind = None
    reaction_removed = False
    if assoc_type in REACTION_ADDED:
        reaction_kind = REACTION_NAMES.get(assoc_type - 2000, "like")
    elif assoc_type in REACTION_REMOVED:
        reaction_kind = REACTION_NAMES.get(assoc_type - 3000, "like")
        reaction_removed = True

    text = (row["text"] or "").strip()
    text_source = "text_column"
    if not text:
        recovered = extract_attributed_body(row["attributed_body"])
        if recovered:
            text, text_source = recovered, "attributed_body"
        elif row["has_attachments"]:
            text_source = "attachment_only"
        else:
            text_source = "empty"

    chat_identifier = row["chat_identifier"] or row["handle"] or "unknown"
    style = row["chat_style"] or 0

    return RawIMessage(
        rowid=int(row["rowid"]),
        guid=row["guid"] or "",
        text=text,
        text_source=text_source,
        sent_at=apple_time_to_datetime(row["date"]),
        is_from_me=bool(row["is_from_me"]),
        service=row["service"] or "iMessage",
        handle=row["handle"] or "",
        chat_id=str(chat_identifier),
        chat_name=row["chat_display_name"] or "",
        # style 43 is a group chat, 45 is 1:1 in Apple's encoding.
        is_group=style == 43,
        has_attachments=bool(row["has_attachments"]),
        is_edited=bool(row["date_edited"]),
        is_unsent=bool(row["date_retracted"]),
        reaction_kind=reaction_kind,
        reaction_removed=reaction_removed,
        reaction_target=(row["assoc_guid"] or "").split("/")[-1],
    )


def max_rowid(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) AS m FROM message").fetchone()
    return int(row["m"] if row else 0)


def text_coverage(messages: List[RawIMessage]) -> float:
    """Fraction of non-reaction messages that yielded text.

    The health check that catches the attributedBody failure. Without it, a
    connector that recovers nothing looks perfectly healthy — it reports
    thousands of rows ingested, all of them blank.
    """
    candidates = [m for m in messages if not m.is_reaction and not m.has_attachments]
    if not candidates:
        return 1.0
    return sum(1 for m in candidates if m.text) / len(candidates)


#: Below this, something is structurally wrong — almost certainly that
#: attributedBody is not being read on a macOS version that requires it.
MIN_TEXT_COVERAGE = 0.5
