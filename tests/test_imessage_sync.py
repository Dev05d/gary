"""iMessage: reading chat.db, and turning it into sessions.

Every test runs against a real SQLite file built by `tests.fake_imessage`, read
through the real connector. Nothing about the reader is stubbed, because the
failures worth catching here are all in the reading.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from backend.connectors.imessage.reader import (
    MIN_TEXT_COVERAGE,
    IMessageUnavailable,
    RawIMessage,
    apple_time_to_datetime,
    extract_attributed_body,
    inspect_schema,
    max_rowid,
    open_readonly_copy,
    read_messages,
    text_coverage,
)
from backend.connectors.imessage.sync import (
    group_into_sessions,
    ingest_batch,
    render_transcript,
    sync_imessage_once,
)
from backend.database.models import (
    Chat,
    ChatMessage,
    ChatSession,
    Reaction,
    Source,
    SyncState,
)
from backend.database.session import get_sessionmaker
from tests.fake_imessage import (
    FakeChatDB,
    build_sample_db,
    make_attributed_body,
    to_apple_ns,
    to_apple_seconds,
)


@pytest_asyncio.fixture
async def source(session):
    src = Source(kind="imessage", display_name="iMessage", account_identifier="local")
    session.add(src)
    await session.commit()
    return src


@pytest.fixture
def sample_db(tmp_path) -> Path:
    return build_sample_db(tmp_path / "chat.db")


def _msg(rowid: int, text: str, minutes: float, *, from_me: bool = False) -> RawIMessage:
    return RawIMessage(
        rowid=rowid, guid=f"G{rowid}", text=text, text_source="text_column",
        sent_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes),
        is_from_me=from_me, service="iMessage", handle="+15551234567",
        chat_id="+15551234567", chat_name="", is_group=False, has_attachments=False,
    )


# ---------------------------------------------------------------------------
# attributedBody — the failure that silently empties the whole corpus
# ---------------------------------------------------------------------------

def test_attributed_body_round_trips_real_text():
    for text in [
        "yes — 7pm at the usual place",
        "ok",
        "👍🏽 see you then",
        "line one\nline two",
        "x" * 5000,
        "quote \" and ' apostrophe",
    ]:
        assert extract_attributed_body(make_attributed_body(text)) == text


def test_short_message_beats_the_attribute_key():
    """The regression that motivates parsing rather than scanning.

    `__kIMMessagePartAttributeName` is 29 characters and sits in every blob, so
    "take the longest string" returns it for any message shorter than that —
    which is most messages.
    """
    blob = make_attributed_body("ok")
    assert extract_attributed_body(blob) == "ok"
    assert "__k" not in (extract_attributed_body(blob) or "")


def test_message_that_looks_like_an_internal_key_is_still_returned():
    assert extract_attributed_body(make_attributed_body("NSString is annoying")) == (
        "NSString is annoying"
    )


def test_attachment_placeholder_alone_is_not_text():
    """A photo-only message is `￼` and nothing else.

    Storing that would put an invisible one-character "message" in the corpus
    and embed it, which is worse than storing nothing.
    """
    assert extract_attributed_body(make_attributed_body("￼")) is None
    assert extract_attributed_body(make_attributed_body("￼check this out")) == (
        "check this out"
    )


def test_text_recovered_before_a_parse_failure_survives_it():
    """Real blobs carry trailing structures this reader rejects."""
    blob = make_attributed_body("survives partial parse", trailing_garbage=True)
    assert extract_attributed_body(blob) == "survives partial parse"


def test_unparseable_blobs_return_none_rather_than_raising():
    for blob in [None, b"", b"\xff\xfe\x00garbage", b"bplist00" + b"\x00" * 40,
                 make_attributed_body("hello there")[:40]]:
        assert extract_attributed_body(blob) is None


# ---------------------------------------------------------------------------
# Apple epoch
# ---------------------------------------------------------------------------

def test_apple_epoch_handles_both_unit_conventions():
    when = datetime(2026, 3, 4, 15, 30, tzinfo=timezone.utc)
    assert apple_time_to_datetime(to_apple_ns(when)) == when
    assert apple_time_to_datetime(to_apple_seconds(when)) == when


def test_implausible_timestamps_are_dropped_not_stored():
    """A message dated 1970 sorts to the front of every query, forever."""
    for value in [0, None, -1e18, 1e30, "not a number"]:
        assert apple_time_to_datetime(value) is None


# ---------------------------------------------------------------------------
# Schema handling
# ---------------------------------------------------------------------------

def test_legacy_schema_without_attributed_body_is_accepted(tmp_path):
    """Pre-Ventura Macs have no `attributedBody`; the text column is enough."""
    db = FakeChatDB(tmp_path / "chat.db", legacy=True)
    db.add_message(chat="+15551234567", handle="+15551234567",
                   when=datetime.now(timezone.utc), text="hello from 2019")
    db.close()

    conn, _ = open_readonly_copy(tmp_path / "chat.db")
    report = inspect_schema(conn)
    assert report.ok and report.version_hint == "legacy"
    assert [m.text for m in read_messages(conn)] == ["hello from 2019"]


def test_unfamiliar_schema_is_refused_rather_than_mis_read(tmp_path):
    """Guessing at renamed columns produces wrong data, not an error."""
    path = tmp_path / "chat.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, body TEXT);")
    conn.commit()
    conn.close()

    conn, _ = open_readonly_copy(path)
    report = inspect_schema(conn)
    assert not report.ok
    assert "guid" in report.missing_columns
    assert "macOS version" in report.message


def test_missing_database_names_the_path_and_the_platform(tmp_path):
    with pytest.raises(IMessageUnavailable) as excinfo:
        open_readonly_copy(tmp_path / "nope.db")
    assert "macOS" in str(excinfo.value)


def test_the_original_database_is_never_touched(sample_db):
    """Messages.app owns the live file; a write handle risks corrupting it."""
    before = sample_db.stat().st_mtime_ns
    digest_before = sample_db.read_bytes()

    conn, copy = open_readonly_copy(sample_db)
    list(read_messages(conn))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM message")  # opened immutable

    assert copy != sample_db
    assert sample_db.read_bytes() == digest_before
    assert sample_db.stat().st_mtime_ns == before


def test_wal_companions_are_copied(tmp_path):
    """Recent messages live in the -wal file until a checkpoint moves them."""
    db = FakeChatDB(tmp_path / "chat.db")
    db.add_message(chat="+1555", handle="+1555", when=datetime.now(timezone.utc), text="hi")
    db.close()
    (tmp_path / "chat.db-wal").write_bytes(b"")
    (tmp_path / "chat.db-shm").write_bytes(b"")

    _, copy = open_readonly_copy(tmp_path / "chat.db")
    assert copy.with_name(copy.name + "-wal").exists()
    assert copy.with_name(copy.name + "-shm").exists()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_reader_routes_every_row_type(sample_db):
    conn, _ = open_readonly_copy(sample_db)
    messages = list(read_messages(conn))
    by_source = {}
    for m in messages:
        by_source.setdefault(m.text_source, []).append(m)

    assert by_source["text_column"], "legacy text column still works"
    assert by_source["attributed_body"], "modern blobs are decoded"
    assert by_source["attachment_only"], "an image with no caption is not a failure"

    reactions = [m for m in messages if m.is_reaction]
    assert {r.reaction_kind for r in reactions} == {"like"}
    assert any(r.reaction_removed for r in reactions)
    assert all(r.reaction_target == "MSG-TARGET-1" for r in reactions)

    assert any(m.is_group for m in messages), "group chats are flagged"
    assert any(m.sent_at is None for m in messages), "garbage dates are dropped"


def test_watermark_only_returns_newer_rows(sample_db):
    conn, _ = open_readonly_copy(sample_db)
    everything = list(read_messages(conn))
    top = max_rowid(conn)
    assert top == max(m.rowid for m in everything)

    tail = list(read_messages(conn, after_rowid=everything[4].rowid))
    assert [m.rowid for m in tail] == [m.rowid for m in everything[5:]]
    assert list(read_messages(conn, after_rowid=top)) == []


def test_text_coverage_ignores_attachment_only_rows():
    """Coverage must measure decoding, not photo messages."""
    photo = _msg(1, "", 0)
    photo.has_attachments = True
    assert text_coverage([photo]) == 1.0

    blanks = [_msg(i, "", i) for i in range(2, 6)]
    assert text_coverage(blanks) == 0.0
    assert text_coverage(blanks + [_msg(9, "text", 9)]) < MIN_TEXT_COVERAGE


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_sessions_split_on_a_gap_not_a_count():
    messages = [
        _msg(1, "you free thursday?", 0),
        _msg(2, "yeah", 3),
        _msg(3, "7pm?", 4),
        # Four hours later — a different conversation.
        _msg(4, "running late", 240),
        _msg(5, "no worries", 242),
    ]
    groups = group_into_sessions(messages, gap_minutes=30)
    assert [len(g) for g in groups] == [3, 2]


def test_a_single_message_is_still_a_session():
    assert len(group_into_sessions([_msg(1, "hey", 0)])) == 1
    assert group_into_sessions([]) == []


def test_undated_messages_never_start_a_session():
    dated = _msg(1, "hello", 0)
    undated = _msg(2, "when?", 1)
    undated.sent_at = None
    groups = group_into_sessions([dated, undated])
    assert [m.rowid for g in groups for m in g] == [1]


def test_transcript_labels_both_sides_and_skips_empties():
    empty = _msg(3, "", 2)
    transcript = render_transcript([
        _msg(1, "dinner friday?", 0),
        _msg(2, "works for me", 1, from_me=True),
        empty,
    ])
    lines = transcript.splitlines()
    assert len(lines) == 2
    assert "Me: works for me" in lines[1]
    assert "+15551234567: dinner friday?" in lines[0]


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

async def test_first_run_records_a_watermark_and_ingests_nothing(session, source, sample_db):
    """Live-only. The user asked for what happens from now on, not an import."""
    outcome = await sync_imessage_once(session, source_id=source.id, db_path=str(sample_db))
    await session.commit()

    assert outcome.ingested == 0
    state = await session.get(SyncState, source.id)
    assert state.watermark["rowid"] == 10
    assert state.recording_since is not None
    assert (await session.execute(select(func.count(ChatMessage.id)))).scalar() == 0


async def test_second_run_ingests_only_what_arrived_after(session, source, sample_db, tmp_path):
    await sync_imessage_once(session, source_id=source.id, db_path=str(sample_db))
    await session.commit()

    # New traffic arrives after the watermark.
    db = sqlite3.connect(sample_db)
    db.close()
    later = datetime.now(timezone.utc)
    appended = FakeChatDB.__new__(FakeChatDB)
    appended.conn = sqlite3.connect(sample_db)
    appended.legacy = False
    appended._handles = {"+15551234567": 1}
    appended._chats = {"+15551234567": 1}
    appended.path = sample_db
    appended.add_message(chat="+15551234567", handle="+15551234567", when=later,
                         attributed="one more thing")
    appended.close()

    outcome = await sync_imessage_once(session, source_id=source.id, db_path=str(sample_db))
    await session.commit()

    assert outcome.ingested == 1
    stored = (await session.execute(select(ChatMessage))).scalars().all()
    assert [m.text for m in stored] == ["one more thing"]
    assert (await session.get(SyncState, source.id)).watermark["rowid"] == 11


async def test_tapbacks_go_to_reactions_not_messages(session, source, sample_db):
    conn, _ = open_readonly_copy(sample_db)
    batch = list(read_messages(conn))
    outcome = await ingest_batch(session, source_id=source.id, messages=batch)
    await session.commit()

    assert outcome.reactions == 2
    texts = [m.text for m in (await session.execute(select(ChatMessage))).scalars()]
    assert not any(t.startswith("Liked ") for t in texts), (
        'a corpus full of \'Liked "ok"\' is worse than no corpus'
    )
    kinds = [r.kind for r in (await session.execute(select(Reaction))).scalars()]
    assert kinds == ["like", "like"]
    removed = [r.removed for r in (await session.execute(select(Reaction))).scalars()]
    assert removed == [False, True]


async def test_sessions_are_built_per_chat(session, source, sample_db):
    conn, _ = open_readonly_copy(sample_db)
    await ingest_batch(session, source_id=source.id, messages=list(read_messages(conn)))
    await session.commit()

    chats = (await session.execute(select(Chat))).scalars().all()
    assert {c.source_chat_id for c in chats} == {
        "+15551234567", "chat987654321", "+15559998888"
    }
    assert any(c.is_group for c in chats)

    sessions = (await session.execute(select(ChatSession))).scalars().all()
    # The two-hour gap in the main chat splits it; a session never spans chats.
    main = [c for c in chats if c.source_chat_id == "+15551234567"][0]
    main_sessions = [s for s in sessions if s.chat_id == main.id]
    assert len(main_sessions) == 2
    assert all(s.transcript for s in sessions)


async def test_re_ingesting_the_same_rows_creates_no_duplicates(session, source, sample_db):
    conn, _ = open_readonly_copy(sample_db)
    batch = list(read_messages(conn))
    first = await ingest_batch(session, source_id=source.id, messages=batch)
    await session.commit()
    second = await ingest_batch(session, source_id=source.id, messages=batch)
    await session.commit()

    assert second.ingested == 0
    total = (await session.execute(select(func.count(ChatMessage.id)))).scalar()
    assert total == first.ingested


async def test_an_unsent_message_is_tombstoned_on_the_next_pass(session, source, tmp_path):
    """Unsend edits a row already ingested; the corpus must follow."""
    path = tmp_path / "chat.db"
    db = FakeChatDB(path)
    when = datetime.now(timezone.utc)
    db.add_message(chat="+1555", handle="+1555", when=when, attributed="oops wrong chat")
    db.close()

    conn, _ = open_readonly_copy(path)
    await ingest_batch(session, source_id=source.id, messages=list(read_messages(conn)))
    await session.commit()

    live = sqlite3.connect(path)
    live.execute("UPDATE message SET date_retracted = ?", (to_apple_ns(when),))
    live.commit()
    live.close()

    conn2, _ = open_readonly_copy(path)
    await ingest_batch(session, source_id=source.id, messages=list(read_messages(conn2)))
    await session.commit()

    stored = (await session.execute(select(ChatMessage))).scalars().all()
    assert len(stored) == 1
    assert stored[0].deleted_at is not None


async def test_blank_corpus_raises_a_loud_warning(session, source, tmp_path):
    """The one failure that otherwise reports perfect health."""
    path = tmp_path / "chat.db"
    db = FakeChatDB(path)
    now = datetime.now(timezone.utc)
    for i in range(6):
        db.add_message(chat="+1555", handle="+1555", when=now + timedelta(minutes=i))
    db.close()

    conn, _ = open_readonly_copy(path)
    outcome = await ingest_batch(session, source_id=source.id, messages=list(read_messages(conn)))
    await session.commit()

    assert outcome.text_coverage == 0.0
    assert outcome.coverage_warning is not None
    assert "attributedBody" in outcome.coverage_warning


async def test_healthy_corpus_raises_no_warning(session, source, sample_db):
    conn, _ = open_readonly_copy(sample_db)
    outcome = await ingest_batch(session, source_id=source.id, messages=list(read_messages(conn)))
    assert outcome.coverage_warning is None
    assert outcome.text_coverage == 1.0


# ---------------------------------------------------------------------------
# Sessions across ingestion batches — the normal case, not an edge case
# ---------------------------------------------------------------------------
#
# The default poll interval is 30 seconds; the default session gap is 30
# minutes. Any live back-and-forth that outlasts one poll tick — which is
# most conversations, since people do not reply within 30 seconds of every
# message — is split across at least two `ingest_batch` calls. Building
# sessions purely from what is in the *current* batch, blind to what the
# chat's most recent session already holds, turns one real conversation into
# several small ones for no reason but when the connector happened to look.

async def test_a_conversation_split_across_two_ticks_is_one_session(session, source):
    tick1 = [_msg(1, "you free thursday?", 0), _msg(2, "yeah 7pm works", 0.33)]
    await ingest_batch(session, source_id=source.id, messages=tick1)
    await session.commit()

    tick2 = [_msg(3, "see you then", 0.66)]  # 20s after the first tick's last message
    await ingest_batch(session, source_id=source.id, messages=tick2)
    await session.commit()

    sessions = (await session.execute(select(ChatSession))).scalars().all()
    assert len(sessions) == 1, "one continuous conversation must not fragment across ticks"
    assert sessions[0].message_count == 3
    assert "you free thursday?" in sessions[0].transcript
    assert "see you then" in sessions[0].transcript


async def test_a_real_gap_after_a_tick_still_starts_a_new_session(session, source):
    """The fix must not glue every message in a chat into one giant session —
    only continue across a tick boundary when the real-world gap is small."""
    tick1 = [_msg(1, "you free thursday?", 0)]
    await ingest_batch(session, source_id=source.id, messages=tick1)
    await session.commit()

    tick2 = [_msg(2, "hey, sorry, missed this — still on?", 45)]  # 45 min later
    await ingest_batch(session, source_id=source.id, messages=tick2)
    await session.commit()

    sessions = (
        await session.execute(select(ChatSession).order_by(ChatSession.started_at))
    ).scalars().all()
    assert len(sessions) == 2
    assert [s.message_count for s in sessions] == [1, 1]


async def test_extension_updates_the_session_boundaries_and_invalidates_summary(session, source):
    tick1 = [_msg(1, "hey", 0)]
    await ingest_batch(session, source_id=source.id, messages=tick1)
    await session.commit()

    first = (await session.execute(select(ChatSession))).scalar_one()
    first.summary = "a stale, already-generated summary"
    await session.commit()

    tick2 = [_msg(2, "you around?", 1)]
    await ingest_batch(session, source_id=source.id, messages=tick2)
    await session.commit()

    await session.refresh(first)
    assert first.message_count == 2
    assert first.ended_at.replace(tzinfo=timezone.utc) == _msg(2, "", 1).sent_at
    assert first.summary is None, "a summary of the old, shorter transcript is now wrong"


async def test_three_ticks_of_one_conversation_still_produce_one_session(session, source):
    for i, (text, minutes) in enumerate([
        ("first", 0), ("second", 0.4), ("third", 0.9), ("fourth", 1.5), ("fifth", 2.1)
    ], start=1):
        await ingest_batch(session, source_id=source.id, messages=[_msg(i, text, minutes)])
        await session.commit()

    sessions = (await session.execute(select(ChatSession))).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].message_count == 5
    for text in ("first", "second", "third", "fourth", "fifth"):
        assert text in sessions[0].transcript


async def test_extension_never_moves_ended_at_backwards(session, source):
    """iCloud sync can deliver an older message after a newer one has already
    been ingested. Extending with it must not un-advance the session's end."""
    tick1 = [_msg(1, "later message", 10)]
    await ingest_batch(session, source_id=source.id, messages=tick1)
    await session.commit()

    session_row = (await session.execute(select(ChatSession))).scalar_one()
    original_ended_at = session_row.ended_at

    tick2 = [_msg(2, "an earlier message, arriving late", 8)]
    await ingest_batch(session, source_id=source.id, messages=tick2)
    await session.commit()

    await session.refresh(session_row)
    assert session_row.ended_at.replace(tzinfo=timezone.utc) >= original_ended_at.replace(
        tzinfo=timezone.utc
    )


async def test_a_much_older_stray_message_does_not_extend_a_recent_session(session, source):
    """A message delivered wildly out of order (hours earlier than the
    session it would otherwise be compared against) must not merge into it —
    the gap check has to be symmetric, not just 'is the new one later'."""
    tick1 = [_msg(1, "recent activity", 100)]
    await ingest_batch(session, source_id=source.id, messages=tick1)
    await session.commit()

    # A message from 3 hours before that session's only entry — e.g. a
    # backfilled or delayed row.
    tick2 = [_msg(2, "a message from ages ago", 100 - 180)]
    await ingest_batch(session, source_id=source.id, messages=tick2)
    await session.commit()

    sessions = (await session.execute(select(ChatSession))).scalars().all()
    assert len(sessions) == 2, "a 3-hour-distant message must start its own session"


async def test_extension_stops_at_the_message_count_cap(session, source):
    """A session that never goes quiet for `gap_minutes` must not grow its
    transcript without bound."""
    from backend.connectors.imessage.sync import MAX_SESSION_MESSAGES

    rowid = 1
    minute = 0.0
    for _ in range(MAX_SESSION_MESSAGES + 5):
        await ingest_batch(
            session, source_id=source.id, messages=[_msg(rowid, f"msg{rowid}", minute)]
        )
        await session.commit()
        rowid += 1
        minute += 0.1  # well within the gap every time

    sessions = (
        await session.execute(select(ChatSession).order_by(ChatSession.started_at))
    ).scalars().all()
    assert len(sessions) == 2, "the cap must force a second session rather than growing forever"
    assert sessions[0].message_count == MAX_SESSION_MESSAGES
    assert sessions[1].message_count == 5


# ---------------------------------------------------------------------------
# ChatMessage concurrency — two overlapping syncs of the same source
# ---------------------------------------------------------------------------

async def test_concurrent_ingestion_of_the_same_new_row_does_not_crash(session, source):
    """A manual 'check now' and the worker's own poll tick can both be
    mid-sync at once, each on its own session, each reading the chat.db
    watermark before the other has advanced it — so both can observe the same
    'new' row. Structured as independent begin/work/commit units, matching
    how two real overlapping syncs behave, rather than two sessions sharing
    one still-open transaction (which deadlocks on SQLite's writer lock
    regardless of whether the upsert logic is correct)."""
    sm = get_sessionmaker()
    raw = [_msg(1, "racing message", 0)]

    async def _one():
        async with sm() as s:
            outcome = await ingest_batch(s, source_id=source.id, messages=raw)
            await s.commit()
            return outcome.ingested

    ingested_counts = await asyncio.gather(*(_one() for _ in range(6)))
    assert sum(ingested_counts) == 1, "exactly one of the racing writers should have won"

    count = await session.scalar(
        select(func.count()).select_from(ChatMessage).where(
            ChatMessage.source_id == source.id, ChatMessage.source_rowid == 1
        )
    )
    assert count == 1


async def test_losing_the_race_does_not_inflate_the_chat_message_count(session, source):
    """`upsert_chat` runs before the message insert (its id is the message's
    foreign key, needed before either racing writer knows who will win) — so
    naively bumping `message_count` there would count once per *attempt*, not
    once per row actually stored. Six racing attempts at one new message must
    still leave the chat's count at 1, not 6."""
    sm = get_sessionmaker()
    raw = [_msg(1, "racing message", 0)]

    async def _one():
        async with sm() as s:
            await ingest_batch(s, source_id=source.id, messages=raw)
            await s.commit()

    await asyncio.gather(*(_one() for _ in range(6)))

    chat = (await session.execute(select(Chat).where(Chat.source_id == source.id))).scalar_one()
    assert chat.message_count == 1, "the race's losers must not each add a phantom count"
