"""Calendar sync, driven through the real client against a fake API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from backend.connectors.calendar.sync import (
    my_response_from,
    organizer_email,
    parse_event_time,
    sync_calendar_once,
    upcoming_events,
    upsert_event,
)
from backend.database.models import CalendarEvent, Identity, Source, SyncState, utcnow
from backend.database.session import get_sessionmaker
from tests.fake_calendar import FakeCalendar, attendee, event


@pytest_asyncio.fixture
async def source(session):
    src = Source(kind="gcal", display_name="Google Calendar",
                 account_identifier="me@gmail.com")
    session.add(src)
    await session.commit()
    return src


def _at(hours: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

def test_all_day_event_keeps_its_local_date():
    """An event on the 15th is on the 15th, in every timezone.

    Converting the date to an instant and formatting it back is the bug this
    guards: west of Greenwich, UTC midnight on the 15th renders as the 14th,
    so a birthday reminder fires a day early, every time.
    """
    instant, all_day, local_date, _ = parse_event_time({"date": "2026-08-15"})
    assert all_day is True
    assert local_date == "2026-08-15"
    assert instant is not None  # still sortable


def test_timed_event_parses_offset_and_zulu():
    a, all_day, local, _ = parse_event_time({"dateTime": "2026-08-15T09:00:00Z"})
    b, _, _, tz = parse_event_time(
        {"dateTime": "2026-08-15T02:00:00-07:00", "timeZone": "America/Los_Angeles"}
    )
    assert all_day is False and local is None
    assert a == b  # same instant, two spellings
    assert tz == "America/Los_Angeles"


def test_malformed_times_are_refused_not_guessed():
    assert parse_event_time({"dateTime": "not a date"})[0] is None
    assert parse_event_time({"date": "15/08/2026"})[0] is None
    assert parse_event_time(None)[0] is None
    assert parse_event_time({})[0] is None


def test_my_response_is_read_from_the_self_attendee():
    declined = event(event_id="e1", attendees=[
        attendee("someone@example.com"),
        attendee("me@gmail.com", response="declined", is_self=True),
    ])
    assert my_response_from(declined) == "declined"
    # An event with no attendee list is one you made for yourself.
    assert my_response_from(event(event_id="e2")) == "accepted"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

async def test_sync_stores_events_and_the_token_only_after_the_last_page(session, source):
    fake = FakeCalendar(pages=[
        [event(event_id=f"a{i}", summary=f"Standup {i}", start=_at(i + 1)) for i in range(3)],
        [event(event_id=f"b{i}", summary=f"Review {i}", start=_at(i + 20)) for i in range(2)],
    ])
    client = fake.client()

    outcome = await sync_calendar_once(session, client, source_id=source.id)
    await session.commit()

    assert outcome.created == 5
    assert outcome.sync_token == "SYNC-2"
    state = await session.get(SyncState, source.id)
    assert state.watermark["sync_token"] == "SYNC-2"

    # Page 2 was actually requested — a single-page read would also report 5
    # if the fake were wrong, so assert the pagination itself.
    assert any(p.get("pageToken") == "1" for p in fake.param_sets)


async def test_expired_sync_token_relists_the_window(session, source):
    session.add(SyncState(source_id=source.id, watermark={"sync_token": "OLD"},
                          recording_since=utcnow(), updated_at=utcnow()))
    await session.commit()

    fake = FakeCalendar(
        pages=[[event(event_id="x1", summary="Kept", start=_at(3))]],
        expired_tokens={"OLD"},
    )
    outcome = await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    assert outcome.used_full_window is True
    assert outcome.created == 1
    # The recovery path must send a window, not the dead token.
    retry = fake.param_sets[-1]
    assert "syncToken" not in retry
    assert "timeMin" in retry and "timeMax" in retry


async def test_recurring_instances_are_expanded_server_side(session, source):
    """`singleEvents=True` is what makes "what do I have Tuesday" an index scan."""
    fake = FakeCalendar(pages=[[]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)
    assert fake.param_sets[0]["singleEvents"] == "true"
    assert fake.param_sets[0]["orderBy"] == "startTime"


async def test_sync_token_request_omits_time_window(session, source):
    """Google rejects `timeMin` alongside `syncToken` with a 400."""
    session.add(SyncState(source_id=source.id, watermark={"sync_token": "GOOD"},
                          recording_since=utcnow(), updated_at=utcnow()))
    await session.commit()

    fake = FakeCalendar(pages=[[]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)

    params = fake.param_sets[0]
    assert params["syncToken"] == "GOOD"
    assert "timeMin" not in params and "timeMax" not in params


async def test_cancelled_event_is_tombstoned_not_deleted(session, source):
    fake = FakeCalendar(pages=[[event(event_id="e9", summary="Lunch", start=_at(5))]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    # Second pass: the same event comes back as a cancellation.
    fake2 = FakeCalendar(pages=[[event(event_id="e9", status="cancelled")]])
    outcome = await sync_calendar_once(session, fake2.client(), source_id=source.id)
    await session.commit()

    assert outcome.cancelled == 1
    stored = (await session.execute(
        select(CalendarEvent).where(CalendarEvent.source_event_id == "e9")
    )).scalar_one()
    # The row survives, so "what happened to lunch?" is still answerable.
    assert stored.deleted_at is not None
    assert stored.title == "Lunch"


async def test_cancelling_an_event_we_never_saw_is_not_an_error(session, source):
    """Tombstones for events outside the window arrive routinely."""
    fake = FakeCalendar(pages=[[event(event_id="never-seen", status="cancelled")]])
    outcome = await sync_calendar_once(session, fake.client(), source_id=source.id)
    assert outcome.errors == []
    assert outcome.skipped == 1


async def test_reschedule_updates_in_place(session, source):
    original = _at(6)
    fake = FakeCalendar(pages=[[event(event_id="r1", summary="1:1", start=original)]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    moved = original + timedelta(hours=2)
    fake2 = FakeCalendar(pages=[[event(event_id="r1", summary="1:1 (moved)", start=moved)]])
    outcome = await sync_calendar_once(session, fake2.client(), source_id=source.id)
    await session.commit()

    assert outcome.updated == 1 and outcome.created == 0
    rows = (await session.execute(
        select(CalendarEvent).where(CalendarEvent.source_event_id == "r1")
    )).scalars().all()
    assert len(rows) == 1, "a reschedule must not create a duplicate event"
    assert rows[0].title == "1:1 (moved)"


async def test_events_without_a_start_are_skipped_not_stored_at_epoch(session, source):
    broken = {"id": "bad", "status": "confirmed", "summary": "No start"}
    fake = FakeCalendar(pages=[[broken]])
    outcome = await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    assert outcome.skipped == 1 and outcome.created == 0
    assert (await session.execute(select(CalendarEvent))).scalars().first() is None


async def test_rate_limiting_is_retried(session, source):
    fake = FakeCalendar(
        pages=[[event(event_id="ok1", summary="Made it", start=_at(2))]],
        rate_limit_times=2,
    )
    outcome = await sync_calendar_once(session, fake.client(), source_id=source.id)
    assert outcome.created == 1


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

async def test_upcoming_excludes_declined_and_cancelled(session, source):
    fake = FakeCalendar(pages=[[
        event(event_id="going", summary="Going", start=_at(4),
              attendees=[attendee("me@gmail.com", response="accepted", is_self=True)]),
        event(event_id="nope", summary="Declined", start=_at(5),
              attendees=[attendee("me@gmail.com", response="declined", is_self=True)]),
        event(event_id="maybe", summary="Tentative", start=_at(6),
              attendees=[attendee("me@gmail.com", response="tentative", is_self=True)]),
    ]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    found = await upcoming_events(session, start=_at(0), end=_at(24))
    titles = {e.title for e in found}
    assert "Going" in titles
    assert "Tentative" in titles, "tentative is still on your calendar"
    assert "Declined" not in titles, "a declined event is not something you have"

    with_declined = await upcoming_events(
        session, start=_at(0), end=_at(24), include_declined=True
    )
    assert len(with_declined) == 3


async def test_upcoming_is_ordered_by_start(session, source):
    fake = FakeCalendar(pages=[[
        event(event_id="late", summary="Late", start=_at(9)),
        event(event_id="early", summary="Early", start=_at(1)),
        event(event_id="mid", summary="Mid", start=_at(5)),
    ]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    found = await upcoming_events(session, start=_at(0), end=_at(24))
    assert [e.title for e in found] == ["Early", "Mid", "Late"]


# ---------------------------------------------------------------------------
# upcoming_events range semantics — overlap, not "starts inside"
# ---------------------------------------------------------------------------

async def test_multiday_event_is_visible_on_every_day_it_spans(session, source):
    """The regression case: `starts_at` alone is on day one. A query for day
    two or three of a multi-day event must still match on `ends_at`, or a
    3-day conference silently disappears from "what do I have tomorrow" for
    two thirds of its own duration.

    Boundaries are derived from one frozen `anchor`, not repeated `_at()`
    calls — `_at()` re-samples `datetime.now()` on every call, and the few
    milliseconds of drift between two calls is exactly the width of the
    boundary this test is trying to pin down.
    """
    anchor = datetime.now(timezone.utc)

    def at(hours: float) -> datetime:
        return anchor + timedelta(hours=hours)

    fake = FakeCalendar(pages=[[
        # Starts tomorrow, runs three full days.
        event(event_id="conf", summary="Conference", start=at(24), duration_minutes=(24 * 3) * 60)
    ]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    day1 = await upcoming_events(session, start=at(24), end=at(48))
    day2 = await upcoming_events(session, start=at(48), end=at(72))
    day3 = await upcoming_events(session, start=at(72), end=at(96))
    before = await upcoming_events(session, start=at(0), end=at(24))
    after = await upcoming_events(session, start=at(96), end=at(120))

    assert [e.title for e in day1] == ["Conference"]
    assert [e.title for e in day2] == ["Conference"], "invisible on its own second day"
    assert [e.title for e in day3] == ["Conference"], "invisible on its own third day"
    assert before == []
    assert after == []


async def test_event_ending_after_the_window_starts_still_matches(session, source):
    """Started before the query range, still running when it begins — an
    overnight event straddling midnight must show up in "today"."""
    started_yesterday = _at(-2)
    fake = FakeCalendar(pages=[[
        event(event_id="overnight", summary="Overnight", start=started_yesterday,
              duration_minutes=6 * 60)  # ends 4 hours from now
    ]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    found = await upcoming_events(session, start=_at(0), end=_at(24))
    assert [e.title for e in found] == ["Overnight"]


async def test_event_ending_exactly_at_window_start_is_excluded(session, source):
    """Open interval at the boundary: an event that finished exactly when the
    query window begins is not "happening" during it. Both the event's end
    and the window's start are derived from one frozen anchor so they land on
    the exact same instant rather than a few milliseconds apart."""
    anchor = datetime.now(timezone.utc)
    ends_exactly_at_window_start = anchor
    fake = FakeCalendar(pages=[[
        event(event_id="just-ended", summary="Just ended",
              start=anchor - timedelta(hours=1), duration_minutes=60)
    ]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    found = await upcoming_events(
        session, start=ends_exactly_at_window_start, end=anchor + timedelta(hours=24)
    )
    assert found == []


async def test_point_events_are_unaffected_by_the_overlap_change(session, source):
    """A plain one-hour meeting must still behave exactly as before: visible
    inside its window, invisible outside it."""
    fake = FakeCalendar(pages=[[
        event(event_id="meeting", summary="1:1", start=_at(5), duration_minutes=30),
    ]])
    await sync_calendar_once(session, fake.client(), source_id=source.id)
    await session.commit()

    assert [e.title for e in await upcoming_events(session, start=_at(0), end=_at(24))] == ["1:1"]
    assert await upcoming_events(session, start=_at(6), end=_at(24)) == []
    assert await upcoming_events(session, start=_at(0), end=_at(4)) == []


async def test_event_with_no_parseable_end_falls_back_to_starts_in_range(session, source):
    """A malformed or absent `end` cannot be overlap-tested — there is no
    known duration. It must still show up by the old rule (starts inside the
    window) rather than vanishing outright or matching everything forever."""
    when = _at(5)
    stored, action = await upsert_event(
        session, source_id=source.id, calendar_id="primary",
        raw={
            "id": "no-end", "status": "confirmed", "summary": "No end time",
            "start": {"dateTime": when.isoformat().replace("+00:00", "Z")},
            # no "end" key at all
        },
    )
    await session.commit()
    assert action == "created"
    assert stored.ends_at is None

    assert [e.title for e in await upcoming_events(session, start=_at(0), end=_at(24))] == [
        "No end time"
    ]
    assert await upcoming_events(session, start=_at(6), end=_at(24)) == []


# ---------------------------------------------------------------------------
# upsert_event concurrency — two syncs of the same source overlapping
# ---------------------------------------------------------------------------

async def test_concurrent_upsert_of_the_same_new_event_does_not_crash(session, source):
    """A manual 'check now' and the worker's own poll tick can both be
    mid-sync for the same source at once. Each opens its own session; both
    see no existing row for a brand-new event, and a plain select-then-insert
    would have the second die on the unique constraint. Structured as two
    independent begin/work/commit units, matching how two real overlapping
    syncs actually behave — not two sessions sharing one still-open
    transaction, which would deadlock on SQLite's own writer lock regardless
    of whether the upsert logic is correct."""
    sm = get_sessionmaker()
    raw = {
        "id": "racing-event", "status": "confirmed", "summary": "Standup",
        "start": {"dateTime": _at(1).isoformat().replace("+00:00", "Z")},
    }

    async def _one():
        async with sm() as s:
            _, action = await upsert_event(s, source_id=source.id, calendar_id="primary", raw=raw)
            await s.commit()
            return action

    actions = await asyncio.gather(*(_one() for _ in range(8)))
    assert set(actions) <= {"created", "updated"}

    count = await session.scalar(
        select(func.count()).select_from(CalendarEvent).where(
            CalendarEvent.source_id == source.id, CalendarEvent.source_event_id == "racing-event"
        )
    )
    assert count == 1, "eight racing upserts of the same event must produce one row"


# ---------------------------------------------------------------------------
# Organizer resolution
# ---------------------------------------------------------------------------

def test_organizer_email_reads_the_organizer_field():
    assert organizer_email({"organizer": {"email": "boss@example.com"}}) == "boss@example.com"
    assert organizer_email({}) is None
    assert organizer_email({"organizer": {}}) is None
    assert organizer_email({"organizer": {"displayName": "No email"}}) is None


async def test_upsert_links_the_organizer_to_an_identity(session, source):
    stored, _ = await upsert_event(
        session, source_id=source.id, calendar_id="primary",
        raw={
            "id": "org1", "status": "confirmed", "summary": "Budget review",
            "organizer": {"email": "boss@example.com", "displayName": "The Boss"},
            "start": {"dateTime": _at(1).isoformat().replace("+00:00", "Z")},
        },
    )
    await session.commit()

    assert stored.organizer_identity_id is not None
    identity = await session.get(Identity, stored.organizer_identity_id)
    assert identity.value_normalized == "boss@example.com"


async def test_event_with_no_organizer_field_is_not_an_error(session, source):
    """A personal event you created yourself has no organizer block at all."""
    stored, action = await upsert_event(
        session, source_id=source.id, calendar_id="primary",
        raw={
            "id": "no-org", "status": "confirmed", "summary": "Personal reminder",
            "start": {"dateTime": _at(1).isoformat().replace("+00:00", "Z")},
        },
    )
    await session.commit()
    assert action == "created"
    assert stored.organizer_identity_id is None


async def test_same_organizer_across_events_resolves_to_one_identity(session, source):
    """The organizer of two different meetings is the same person, not two."""
    for event_id in ("m1", "m2"):
        await upsert_event(
            session, source_id=source.id, calendar_id="primary",
            raw={
                "id": event_id, "status": "confirmed", "summary": "Meeting",
                "organizer": {"email": "boss@example.com", "displayName": "Boss"},
                "start": {"dateTime": _at(1).isoformat().replace("+00:00", "Z")},
            },
        )
    await session.commit()

    count = await session.scalar(
        select(func.count()).select_from(Identity).where(
            Identity.value_normalized == "boss@example.com"
        )
    )
    assert count == 1
