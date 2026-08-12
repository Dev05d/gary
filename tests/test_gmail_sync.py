"""Gmail sync: watermarks, pagination, recovery, idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from backend.connectors.gmail.client import (
    GmailClient,
    HistoryExpired,
    TokenBucket,
    collect_history,
)
from backend.connectors.gmail.sync import (
    establish_watermark,
    ingest_message,
    sync_once,
)
from backend.database.models import Identity, Message, MessageThread, Source, SyncState
from backend.pipeline.label_policy import LabelPolicy
from tests.fake_gmail import FakeGmail, build_raw_message

POLICY = LabelPolicy.from_settings("default")
MY = {"me@gmail.com"}


@pytest_asyncio.fixture
async def source(session):
    src = Source(kind="gmail", display_name="Gmail", account_identifier="me@gmail.com")
    session.add(src)
    await session.commit()
    return src


@pytest_asyncio.fixture
async def gmail():
    return FakeGmail()


async def make_client(fake: FakeGmail) -> GmailClient:
    # A fast bucket so tests do not sit in the limiter.
    return GmailClient(
        "token",
        client=fake.client(),
        bucket=TokenBucket(capacity=1e9, refill_per_second=1e9, tokens=1e9),
    )


# ===========================================================================
# Watermark
# ===========================================================================

async def test_connect_records_a_watermark_and_ingests_nothing(session, source, gmail):
    """Live-only: connecting starts the clock, it does not import a mailbox."""
    gmail.add_message(build_raw_message(message_id="old1"))
    client = await make_client(gmail)

    outcome = await sync_once(
        session, client, source_id=source.id, policy=POLICY, my_addresses=MY
    )
    await session.commit()

    assert outcome.ingested == 0, "connecting must not backfill"
    state = await session.get(SyncState, source.id)
    assert state.watermark["history_id"]
    assert state.recording_since is not None, "the data horizon must be recorded"


async def test_only_mail_after_the_watermark_is_ingested(session, source, gmail):
    gmail.add_message(build_raw_message(message_id="before"))
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.add_message(build_raw_message(message_id="after", subject="New mail"))
    outcome = await sync_once(
        session, client, source_id=source.id, policy=POLICY, my_addresses=MY
    )
    await session.commit()

    assert outcome.ingested == 1
    stored = (await session.execute(select(Message))).scalars().all()
    assert [m.source_message_id for m in stored] == ["after"]


# ===========================================================================
# Pagination — the silent data-loss bug
# ===========================================================================

async def test_every_history_page_is_walked(session, source, gmail):
    """Advancing the watermark per page would skip pages 2+ with no error."""
    gmail.page_size = 2
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    for i in range(7):
        gmail.add_message(build_raw_message(message_id=f"m{i}", subject=f"Message {i}"))

    outcome = await sync_once(
        session, client, source_id=source.id, policy=POLICY, my_addresses=MY
    )
    await session.commit()

    assert outcome.ingested == 7, "messages beyond the first page were dropped"


async def test_collect_history_returns_the_final_watermark_only(gmail):
    """The watermark must come from the end of the walk, not from page one."""
    gmail.page_size = 1
    client = await make_client(gmail)
    start = str(gmail.current_history_id)
    for i in range(5):
        gmail.add_message(build_raw_message(message_id=f"h{i}"))

    records, watermark = await collect_history(client, start)
    assert len(records) == 5
    assert int(watermark) == gmail.current_history_id


# ===========================================================================
# Idempotency
# ===========================================================================

async def test_replaying_a_sync_creates_no_duplicates(session, source, gmail):
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    watermark_before = (await session.get(SyncState, source.id)).watermark["history_id"]
    gmail.add_message(build_raw_message(message_id="dup1"))

    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    # Simulate a crash: rewind the watermark and sync the same window again.
    state = await session.get(SyncState, source.id)
    state.watermark = {"history_id": watermark_before}
    await session.commit()

    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    count = await session.scalar(
        select(func.count()).select_from(Message).where(Message.source_message_id == "dup1")
    )
    assert count == 1, "re-reading the same window duplicated a message"


# ===========================================================================
# History expiry
# ===========================================================================

async def test_expired_history_recovers_without_a_full_sync(session, source, gmail):
    """The documented remedy is a full sync; that would import everything."""
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.add_message(build_raw_message(message_id="gap1", subject="During the gap"))
    gmail.expire_history_before(gmail.current_history_id + 1000)

    outcome = await sync_once(
        session, client, source_id=source.id, policy=POLICY, my_addresses=MY
    )
    await session.commit()

    assert outcome.used_recovery is True
    assert outcome.ingested >= 1
    assert "q=" in " ".join(gmail.request_log) or any(
        "messages" in p for p in gmail.request_log
    ), "recovery should use a bounded messages.list query"


async def test_history_expiry_raises_a_typed_error(gmail):
    client = await make_client(gmail)
    gmail.expire_history_before(999_999)
    with pytest.raises(HistoryExpired):
        await collect_history(client, "1")


# ===========================================================================
# Label policy
# ===========================================================================

async def test_promotions_are_skipped(session, source, gmail):
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.add_message(
        build_raw_message(message_id="promo", labels=["INBOX", "CATEGORY_PROMOTIONS"])
    )
    gmail.add_message(build_raw_message(message_id="real", labels=["INBOX"]))

    outcome = await sync_once(
        session, client, source_id=source.id, policy=POLICY, my_addresses=MY
    )
    await session.commit()

    assert outcome.ingested == 1
    assert outcome.skipped_by_policy == 1


async def test_drafts_are_never_ingested(session, source, gmail):
    """A draft was never sent; reporting it would be a fabrication."""
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.add_message(build_raw_message(message_id="draft1", labels=["DRAFT"]))
    outcome = await sync_once(
        session, client, source_id=source.id, policy=POLICY, my_addresses=MY
    )
    await session.commit()
    assert outcome.ingested == 0


async def test_sent_mail_is_ingested_and_flagged(session, source, gmail):
    """Load-bearing for 'who haven't I replied to' and own-promise tracking."""
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.add_message(
        build_raw_message(
            message_id="sent1",
            labels=["SENT"],
            sender="Me <me@gmail.com>",
            to="sarah@example.com",
            body="I'll send the report by Friday.",
        )
    )
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    msg = (
        await session.execute(select(Message).where(Message.source_message_id == "sent1"))
    ).scalar_one()
    assert msg.is_from_me is True


# ===========================================================================
# Normalisation
# ===========================================================================

async def test_message_is_normalised_into_the_facts_plane(session, source, gmail):
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.add_message(
        build_raw_message(
            message_id="rich",
            sender="Prof. John Smith <j.smith@university.edu>",
            to="me@gmail.com, Alex <alex@corp.com>",
            subject="Re: Research Proposal",
            body=(
                "The proposal is due Friday at 5pm.\n\n"
                "Best,\nJohn\n\n"
                "On Mon, Alex wrote:\n> When is it due?\n"
            ),
        )
    )
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    msg = (
        await session.execute(select(Message).where(Message.source_message_id == "rich"))
    ).scalar_one()

    assert msg.subject_normalized == "Research Proposal"
    assert "due Friday at 5pm" in msg.body_clean
    assert "Alex wrote" not in msg.body_clean, "quoted reply leaked into the clean body"
    assert msg.signature_block and "John" in msg.signature_block
    assert len(msg.to_ids) == 2, "a recipient was dropped"

    sender = await session.get(Identity, msg.from_identity_id)
    assert sender.value_normalized == "j.smith@university.edu"
    assert "Prof. John Smith" in (sender.display_names or [])


async def test_thread_tracks_inbound_and_outbound(session, source, gmail):
    """The denormalised columns behind 'who haven't I replied to'."""
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    base = int(datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
    gmail.add_message(
        build_raw_message(message_id="in1", thread_id="T1", received_ms=base)
    )
    gmail.add_message(
        build_raw_message(
            message_id="out1",
            thread_id="T1",
            labels=["SENT"],
            sender="Me <me@gmail.com>",
            received_ms=base + 3_600_000,
        )
    )
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    thread = (
        await session.execute(
            select(MessageThread).where(MessageThread.source_thread_id == "T1")
        )
    ).scalar_one()

    assert thread.message_count == 2
    assert thread.last_inbound_at is not None
    assert thread.last_outbound_at is not None
    assert thread.last_outbound_at > thread.last_inbound_at, "I replied last"


async def test_awaiting_reply_query_works(session, source, gmail):
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    base = int(datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
    gmail.add_message(build_raw_message(message_id="a1", thread_id="TA", received_ms=base))
    gmail.add_message(
        build_raw_message(
            message_id="b1", thread_id="TB", received_ms=base, sender="Bob <bob@x.com>"
        )
    )
    gmail.add_message(
        build_raw_message(
            message_id="b2",
            thread_id="TB",
            labels=["SENT"],
            sender="Me <me@gmail.com>",
            received_ms=base + 60_000,
        )
    )
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    awaiting = (
        await session.execute(
            select(MessageThread).where(
                MessageThread.last_outbound_at.is_(None)
                | (MessageThread.last_inbound_at > MessageThread.last_outbound_at)
            )
        )
    ).scalars().all()

    assert {t.source_thread_id for t in awaiting} == {"TA"}


# ===========================================================================
# Resilience
# ===========================================================================

async def test_one_broken_message_does_not_abort_the_batch(session, source, gmail):
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.add_message(build_raw_message(message_id="good1"))
    # A history record pointing at a message that cannot be fetched.
    gmail.history.append(
        (gmail._next_history_id(), {"messagesAdded": [{"message": {"id": "missing"}}]})
    )
    gmail.add_message(build_raw_message(message_id="good2"))

    outcome = await sync_once(
        session, client, source_id=source.id, policy=POLICY, my_addresses=MY
    )
    await session.commit()

    assert outcome.ingested == 2
    assert any("missing" in e for e in outcome.errors)


async def test_rate_limits_are_retried(session, source, gmail):
    gmail.inject_failures = [429, 403]
    client = await make_client(gmail)
    outcome = await sync_once(
        session, client, source_id=source.id, policy=POLICY, my_addresses=MY
    )
    await session.commit()
    assert outcome.complete is True


async def test_deletion_is_mirrored(session, source, gmail):
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.add_message(build_raw_message(message_id="del1"))
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.delete_message("del1")
    outcome = await sync_once(
        session, client, source_id=source.id, policy=POLICY, my_addresses=MY
    )
    await session.commit()

    assert outcome.deleted == 1
    msg = (
        await session.execute(select(Message).where(Message.source_message_id == "del1"))
    ).scalar_one()
    assert msg.deleted_at is not None


async def test_label_change_updates_rather_than_duplicates(session, source, gmail):
    client = await make_client(gmail)
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.add_message(build_raw_message(message_id="lbl1", labels=["INBOX", "UNREAD"]))
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    gmail.change_labels("lbl1", ["STARRED"])
    await sync_once(session, client, source_id=source.id, policy=POLICY, my_addresses=MY)
    await session.commit()

    count = await session.scalar(
        select(func.count()).select_from(Message).where(Message.source_message_id == "lbl1")
    )
    assert count == 1
    msg = (
        await session.execute(select(Message).where(Message.source_message_id == "lbl1"))
    ).scalar_one()
    assert "STARRED" in msg.labels
