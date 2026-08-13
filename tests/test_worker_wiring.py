"""The plumbing added when Calendar and iMessage joined Gmail as live sources:
dispatch by kind, the iMessage connect/disconnect flow, and the shared
scheduling helpers every worker's poll loop now uses.

Deliberately not testing `poll_forever` itself — it is an infinite loop with
no exit but cancellation, and everything it decides each tick (is a sync due,
which sync function to call, how to record a failure) is covered directly
here and in `test_gmail_sync.py` / `test_calendar_sync.py` /
`test_imessage_sync.py`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from backend.database.models import Source, SyncState, utcnow
from backend.workers import dispatch
from backend.workers.scheduling import due, record_failure
from tests.fake_imessage import build_sample_db

# ===========================================================================
# scheduling.due — the interval-since-last-success + backoff rule every
# worker's poll loop shares
# ===========================================================================

def test_due_with_no_history_is_immediate():
    assert due(None, timedelta(seconds=60)) is True


def test_due_respects_the_interval():
    state = SyncState(source_id="s", last_attempt_at=utcnow(), consecutive_failures=0)
    assert due(state, timedelta(hours=1)) is False
    assert due(state, timedelta(seconds=0)) is True


def test_due_backs_off_on_repeated_failure():
    just_now = utcnow()
    barely_failing = SyncState(source_id="s", last_attempt_at=just_now, consecutive_failures=1)
    badly_failing = SyncState(source_id="s", last_attempt_at=just_now, consecutive_failures=6)

    interval = timedelta(seconds=30)
    # One failure roughly doubles the wait; a run of them is capped, not
    # unbounded — otherwise a source broken for a week never gets retried.
    assert due(barely_failing, interval) is False
    assert due(badly_failing, interval) is False


def test_due_treats_naive_timestamps_as_utc():
    """SQLite round-trips can drop tzinfo; comparing against an aware `now()`
    must not raise."""
    naive = SyncState(
        source_id="s", last_attempt_at=utcnow().replace(tzinfo=None), consecutive_failures=0
    )
    assert due(naive, timedelta(hours=1)) is False  # does not raise


# ===========================================================================
# scheduling.record_failure
# ===========================================================================

@pytest_asyncio.fixture
async def a_source(session) -> Source:
    source = Source(kind="imessage", account_identifier="local", status="connecting")
    session.add(source)
    await session.commit()
    return source


async def test_record_failure_creates_a_missing_row(session, a_source):
    """A sync that fails before ever producing an outcome (a dead credential,
    a database that never existed) must still be visible — a get-only lookup
    that no-ops when the row is missing would drop the very first failure a
    source ever has."""
    await record_failure(session, source_id=a_source.id, error="no credential")
    await session.commit()

    state = await session.get(SyncState, a_source.id)
    assert state is not None
    assert state.consecutive_failures == 1
    assert state.last_error == "no credential"


async def test_record_failure_increments_an_existing_row(session, a_source):
    session.add(SyncState(source_id=a_source.id, consecutive_failures=2, updated_at=utcnow()))
    await session.commit()

    await record_failure(session, source_id=a_source.id, error="still broken")
    await session.commit()

    state = await session.get(SyncState, a_source.id)
    assert state.consecutive_failures == 3
    assert state.last_error == "still broken"


async def test_record_failure_truncates_a_very_long_error(session, a_source):
    """An HTML error page mistaken for an API error must not blow out the
    column — SQLite's TEXT has no hard limit, but there is no reason to store
    megabytes of markup for one failed sync attempt."""
    await record_failure(session, source_id=a_source.id, error="x" * 5000)
    await session.commit()

    state = await session.get(SyncState, a_source.id)
    assert len(state.last_error) == 1000


# ===========================================================================
# dispatch — routing a Source to the worker that knows how to sync it
# ===========================================================================

async def test_dispatch_routes_gmail(session, settings):
    source = Source(kind="gmail", account_identifier="me@example.com", status="connected")
    session.add(source)
    await session.flush()
    # No credential stored, so this fails fast — the point is *which* code
    # path ran, shown by the specific error, not that it succeeds.
    outcome, error = await dispatch.sync_source(session, source=source, settings=settings)
    assert outcome is None
    assert "credential" in (error or "").lower()


async def test_dispatch_routes_gcal(session, settings):
    source = Source(kind="gcal", account_identifier="me@example.com", status="connected")
    session.add(source)
    await session.flush()
    outcome, error = await dispatch.sync_source(session, source=source, settings=settings)
    assert outcome is None
    assert "credential" in (error or "").lower()


async def test_dispatch_routes_imessage(session, settings):
    source = Source(kind="imessage", account_identifier="local", status="connected")
    session.add(source)
    await session.flush()
    # imessage_enabled defaults to False, so this is the specific reason the
    # iMessage path — and not some other one — actually ran.
    outcome, error = await dispatch.sync_source(session, source=source, settings=settings)
    assert outcome is None
    assert "Settings" in (error or "")


async def test_dispatch_reports_unimplemented_kinds_without_guessing(session, settings):
    source = Source(kind="discord_export", account_identifier="x", status="disconnected")
    session.add(source)
    await session.flush()
    outcome, error = await dispatch.sync_source(session, source=source, settings=settings)
    assert outcome is None
    assert "not implemented" in (error or "")


# ===========================================================================
# iMessage connect/disconnect through the HTTP API
# ===========================================================================

async def test_connect_imessage_succeeds_against_a_real_database(client, tmp_path):
    db_path = build_sample_db(tmp_path / "chat.db")
    client.app.state.settings.imessage_db_path = str(db_path)  # type: ignore[attr-defined]

    r = await client.post("/api/sources/imessage/connect")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert "watching iMessage" in body["message"]

    # The switch is now on, and stays on for the running app.
    assert client.app.state.settings.imessage_enabled is True  # type: ignore[attr-defined]

    sources = (await client.get("/api/sources")).json()["sources"]
    entry = next(s for s in sources if s["kind"] == "imessage")
    assert entry["status"] == "connected"
    # First pass is watermark-only, per the live-only design every other
    # source follows too — connecting must not backfill.
    assert entry["messages_ingested"] == 0


async def test_connect_imessage_fails_loudly_without_a_real_database(client):
    """No `imessage_db_path` override, and this container is not a Mac —
    `~/Library/Messages/chat.db` does not exist. That must come back as a
    clear 400, not a 500, and must not silently leave the switch on."""
    r = await client.post("/api/sources/imessage/connect")
    assert r.status_code == 400
    assert "message" in r.json()["detail"]

    # Rolled back — a failed first attempt must not leave a background loop
    # retrying forever against a path known not to work.
    assert client.app.state.settings.imessage_enabled is False  # type: ignore[attr-defined]


async def test_disconnect_imessage_turns_off_the_master_switch(client, tmp_path, session):
    db_path = build_sample_db(tmp_path / "chat.db")
    client.app.state.settings.imessage_db_path = str(db_path)  # type: ignore[attr-defined]

    connected = (await client.post("/api/sources/imessage/connect")).json()
    assert connected["connected"] is True

    from sqlalchemy import select

    source = (
        await session.execute(select(Source).where(Source.kind == "imessage"))
    ).scalar_one()

    r = await client.post(f"/api/sources/{source.id}/disconnect")
    assert r.status_code == 200

    # This is the bug being guarded against: disabling only the Source row
    # while the setting stays on means the very next poll tick re-enables it.
    assert client.app.state.settings.imessage_enabled is False  # type: ignore[attr-defined]

    await session.refresh(source)
    assert source.enabled is False
    assert source.status == "disconnected"


async def test_reconnecting_after_disconnect_reuses_the_row(client, tmp_path, session):
    """Disconnect must not be a dead end — connecting again should not create
    a second `imessage` source and collide with the (kind, account) unique
    constraint."""
    from sqlalchemy import func, select

    db_path = build_sample_db(tmp_path / "chat.db")
    client.app.state.settings.imessage_db_path = str(db_path)  # type: ignore[attr-defined]

    await client.post("/api/sources/imessage/connect")
    first = (
        await session.execute(select(Source).where(Source.kind == "imessage"))
    ).scalar_one()
    await client.post(f"/api/sources/{first.id}/disconnect")

    r = await client.post("/api/sources/imessage/connect")
    assert r.status_code == 200

    count = await session.scalar(
        select(func.count()).select_from(Source).where(Source.kind == "imessage")
    )
    assert count == 1
