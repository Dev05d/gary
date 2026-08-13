"""Synthetic iMessage fixtures: a real chat.db and real attributedBody blobs.

The whole iMessage connector is untestable without these. A Mac is not
available in CI, and even on a Mac the developer's own message history is a
terrible test corpus — it is private, it is not reproducible, and it does not
contain the edge cases on purpose.

Two things are built here:

1. **A typedstream *writer*.** `pytypedstream` only reads. Without a writer
   there is no way to produce a valid `attributedBody` blob, and a hand-typed
   byte string is not a test of anything — it either fails to parse (so the
   test proves nothing) or accidentally parses (so the test proves less). The
   encoder is the exact inverse of `typedstream.stream.TypedStreamReader`, so
   a blob it produces is one the real reader accepts.
2. **A synthetic chat.db** with the subset of Apple's schema the reader uses,
   populated to order: text in the `text` column, text only in
   `attributedBody`, tapbacks, edits, retractions, group chats, and rows whose
   timestamps are nonsense.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

_TAG_INTEGER_2 = -127
_TAG_INTEGER_4 = -126
_TAG_NEW = -124
_TAG_NIL = -123
_TAG_END_OF_OBJECT = -122
_FIRST_TAG = -128
_LAST_TAG = -111
_FIRST_REFERENCE_NUMBER = _LAST_TAG + 1


# ---------------------------------------------------------------------------
# Typedstream writer
# ---------------------------------------------------------------------------

class TypedStreamWriter:
    """Writes the NSArchiver typedstream format, little-endian.

    Deliberately does not deduplicate shared strings. The reader appends every
    literal to its table and resolves references against it, so writing every
    string literally is valid — just larger than what Apple emits. Correctness
    over compactness: this is a fixture, not a wire format.
    """

    def __init__(self) -> None:
        self.buf = bytearray()
        self._header()

    # ------------------------------------------------------------- low level
    def _header(self) -> None:
        self.buf.append(4)  # streamer version
        self.buf.append(11)  # signature length
        self.buf += b"streamtyped"  # little-endian signature
        self.write_integer(1000, signed=False)  # system version

    def write_integer(self, value: int, *, signed: bool) -> None:
        if signed:
            fits = _FIRST_REFERENCE_NUMBER <= value <= 127
        else:
            # A single unsigned byte is ambiguous when its signed reading lands
            # in the tag range, so those values must take the long form.
            fits = 0 <= value <= 0xFF and not (_FIRST_TAG <= value - 256 <= _LAST_TAG)
        if fits:
            self.buf += (value if value >= 0 else value + 256).to_bytes(1, "little")
        elif -(2**15) <= value < 2**15 or (not signed and value < 2**16):
            self.buf += _TAG_INTEGER_2.to_bytes(1, "little", signed=True)
            self.buf += value.to_bytes(2, "little", signed=signed)
        else:
            self.buf += _TAG_INTEGER_4.to_bytes(1, "little", signed=True)
            self.buf += value.to_bytes(4, "little", signed=signed)

    def _tag(self, tag: int) -> None:
        self.buf += tag.to_bytes(1, "little", signed=True)

    def write_unshared_string(self, data: Optional[bytes]) -> None:
        if data is None:
            self._tag(_TAG_NIL)
            return
        self.write_integer(len(data), signed=False)
        self.buf += data

    def write_shared_string(self, data: Optional[bytes]) -> None:
        if data is None:
            self._tag(_TAG_NIL)
            return
        self._tag(_TAG_NEW)
        self.write_unshared_string(data)

    def write_nil(self) -> None:
        self._tag(_TAG_NIL)

    # ------------------------------------------------------------ structures
    def begin_typed_values(self, encoding: bytes) -> None:
        self.write_shared_string(encoding)

    def write_class_chain(self, chain: Sequence[tuple]) -> None:
        """`[(b"NSMutableString", 1), (b"NSString", 1), (b"NSObject", 0)]`."""
        for name, version in chain:
            self._tag(_TAG_NEW)
            self.write_shared_string(name)
            self.write_integer(version, signed=True)
        self.write_nil()  # terminates the chain

    def begin_object(self, chain: Sequence[tuple]) -> None:
        self._tag(_TAG_NEW)
        self.write_class_chain(chain)

    def end_object(self) -> None:
        self._tag(_TAG_END_OF_OBJECT)

    def write_string_object(self, text: str, *, mutable: bool = True) -> None:
        """An NSString/NSMutableString as an `@` value."""
        chain = [(b"NSMutableString", 1), (b"NSString", 1), (b"NSObject", 0)]
        if not mutable:
            chain = chain[1:]
        self.begin_object(chain)
        self.begin_typed_values(b"+")
        self.write_unshared_string(text.encode("utf-8"))
        self.end_object()

    def write_int_value(self, value: int, encoding: bytes = b"i") -> None:
        self.begin_typed_values(encoding)
        self.write_integer(value, signed=True)

    @property
    def data(self) -> bytes:
        return bytes(self.buf)


def make_attributed_body(
    text: str,
    *,
    attribute_keys: Sequence[str] = ("__kIMMessagePartAttributeName",),
    trailing_garbage: bool = False,
) -> bytes:
    """A blob shaped like the one Messages.app writes.

    The attribute-run strings matter: they are `NSString` objects too, so a
    naive "take the longest string" extractor picks
    `__kIMMessagePartAttributeName` over a short real message like "ok".
    """
    w = TypedStreamWriter()
    w.begin_typed_values(b"@")
    w.begin_object(
        [
            (b"NSMutableAttributedString", 0),
            (b"NSAttributedString", 0),
            (b"NSObject", 0),
        ]
    )
    # The text itself, first — this is the value the extractor must return.
    w.begin_typed_values(b"@")
    w.write_string_object(text)
    # Then the attribute runs, as Messages.app writes them.
    w.write_int_value(len(attribute_keys))
    for key in attribute_keys:
        w.begin_typed_values(b"@")
        w.write_string_object(key, mutable=False)
        w.write_int_value(0)
    w.end_object()

    if trailing_garbage:
        # Real blobs routinely carry structures this reader chokes on partway
        # through. Anything already recovered must survive that.
        w.buf += b"\x84\x99\x99\x99\x99"
    return w.data


# ---------------------------------------------------------------------------
# Synthetic chat.db
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE handle (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    service TEXT
);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT,
    chat_identifier TEXT,
    display_name TEXT,
    style INTEGER,
    service_name TEXT
);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT,
    text TEXT,
    attributedBody BLOB,
    date INTEGER,
    date_edited INTEGER DEFAULT 0,
    date_retracted INTEGER DEFAULT 0,
    is_from_me INTEGER DEFAULT 0,
    service TEXT,
    associated_message_type INTEGER DEFAULT 0,
    associated_message_guid TEXT,
    cache_has_attachments INTEGER DEFAULT 0,
    handle_id INTEGER
);
CREATE TABLE chat_message_join (
    chat_id INTEGER,
    message_id INTEGER,
    message_date INTEGER
);
"""

#: The legacy layout: no attributedBody, no edits, no retractions. Sync must
#: degrade to the text column rather than refuse.
LEGACY_SCHEMA = SCHEMA.replace("    attributedBody BLOB,\n", "").replace(
    "    date_edited INTEGER DEFAULT 0,\n", ""
).replace("    date_retracted INTEGER DEFAULT 0,\n", "")


def to_apple_ns(when: datetime) -> int:
    return int((when - APPLE_EPOCH).total_seconds() * 1e9)


def to_apple_seconds(when: datetime) -> int:
    return int((when - APPLE_EPOCH).total_seconds())


class FakeChatDB:
    """Builds a chat.db on disk that the real reader opens unmodified."""

    def __init__(self, path: Path, *, legacy: bool = False) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(LEGACY_SCHEMA if legacy else SCHEMA)
        self.legacy = legacy
        self._handles: Dict[str, int] = {}
        self._chats: Dict[str, int] = {}

    def handle(self, address: str) -> int:
        if address not in self._handles:
            cur = self.conn.execute(
                "INSERT INTO handle (id, service) VALUES (?, 'iMessage')", (address,)
            )
            self._handles[address] = int(cur.lastrowid)
        return self._handles[address]

    def chat(self, identifier: str, *, display_name: str = "", group: bool = False) -> int:
        if identifier not in self._chats:
            cur = self.conn.execute(
                "INSERT INTO chat (guid, chat_identifier, display_name, style, service_name)"
                " VALUES (?, ?, ?, ?, 'iMessage')",
                (f"iMessage;-;{identifier}", identifier, display_name, 43 if group else 45),
            )
            self._chats[identifier] = int(cur.lastrowid)
        return self._chats[identifier]

    def add_message(
        self,
        *,
        chat: str,
        handle: str,
        when: datetime,
        text: Optional[str] = None,
        attributed: Optional[str] = None,
        is_from_me: bool = False,
        assoc_type: int = 0,
        assoc_guid: Optional[str] = None,
        has_attachments: bool = False,
        edited: bool = False,
        retracted: bool = False,
        group: bool = False,
        seconds_epoch: bool = False,
        raw_date: Optional[int] = None,
        guid: Optional[str] = None,
    ) -> int:
        chat_row = self.chat(chat, group=group)
        handle_row = self.handle(handle)
        stamp = (
            raw_date
            if raw_date is not None
            else (to_apple_seconds(when) if seconds_epoch else to_apple_ns(when))
        )
        body = make_attributed_body(attributed) if attributed else None

        columns = [
            "guid", "text", "date", "is_from_me", "service",
            "associated_message_type", "associated_message_guid",
            "cache_has_attachments", "handle_id",
        ]
        values: List[Any] = [
            guid or f"MSG-{len(self._handles)}-{stamp}",
            text,
            stamp,
            int(is_from_me),
            "iMessage",
            assoc_type,
            assoc_guid,
            int(has_attachments),
            handle_row,
        ]
        if not self.legacy:
            columns += ["attributedBody", "date_edited", "date_retracted"]
            values += [body, to_apple_ns(when) if edited else 0, to_apple_ns(when) if retracted else 0]

        cur = self.conn.execute(
            f"INSERT INTO message ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            values,
        )
        rowid = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id, message_date) VALUES (?, ?, ?)",
            (chat_row, rowid, stamp),
        )
        return rowid

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def build_sample_db(path: Path, *, base: Optional[datetime] = None) -> Path:
    """A corpus with one of every failure mode in it."""
    base = base or (datetime.now(timezone.utc) - timedelta(hours=3))
    db = FakeChatDB(path)

    # Legacy path: text in the text column.
    db.add_message(chat="+15551234567", handle="+15551234567", when=base,
                   text="are we still on for thursday?")
    # Modern path: text only in attributedBody.
    db.add_message(chat="+15551234567", handle="+15551234567",
                   when=base + timedelta(minutes=1),
                   attributed="yes — 7pm at the usual place")
    db.add_message(chat="+15551234567", handle="+15551234567",
                   when=base + timedelta(minutes=2),
                   attributed="ok", is_from_me=True)
    # A tapback on the previous message — not a message.
    db.add_message(chat="+15551234567", handle="+15551234567",
                   when=base + timedelta(minutes=3),
                   text='Liked "ok"', assoc_type=2000,
                   assoc_guid="p:0/MSG-TARGET-1")
    # A removed tapback.
    db.add_message(chat="+15551234567", handle="+15551234567",
                   when=base + timedelta(minutes=4),
                   text='Removed a like from "ok"', assoc_type=3000,
                   assoc_guid="p:0/MSG-TARGET-1")
    # Attachment with no text at all — legitimately empty, not a failure.
    db.add_message(chat="+15551234567", handle="+15551234567",
                   when=base + timedelta(minutes=5), has_attachments=True)
    # A new burst, two hours later: a separate session.
    db.add_message(chat="+15551234567", handle="+15551234567",
                   when=base + timedelta(hours=2),
                   attributed="running late, 7:20?")
    # Group chat.
    db.add_message(chat="chat987654321", handle="alex@example.com",
                   when=base + timedelta(minutes=10), group=True,
                   attributed="dinner friday?")
    # Old macOS wrote seconds, not nanoseconds.
    db.add_message(chat="+15559998888", handle="+15559998888",
                   when=base + timedelta(minutes=11),
                   text="seconds-epoch row", seconds_epoch=True)
    # Garbage timestamp: must be dropped, not stored as 1970.
    db.add_message(chat="+15559998888", handle="+15559998888", when=base,
                   text="unparseable date", raw_date=0)
    db.close()
    return path
