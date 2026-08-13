"""Calendar event normalisation and sync."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.calendar.client import (
    CalendarClient,
    SyncTokenExpired,
    collect_events,
)
from backend.database.models import CalendarEvent, SyncState, new_id, utcnow

log = logging.getLogger(__name__)


@dataclass
class CalendarSyncOutcome:
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    used_full_window: bool = False
    sync_token: Optional[str] = None

    def summary(self) -> str:
        bits = []
        if self.created:
            bits.append(f"{self.created} new")
        if self.updated:
            bits.append(f"{self.updated} updated")
        if self.cancelled:
            bits.append(f"{self.cancelled} cancelled")
        if self.used_full_window:
            bits.append("full window re-listed")
        return ", ".join(bits) or "no changes"


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

def parse_event_time(value: Optional[Dict[str, Any]]) -> Tuple[Optional[datetime], bool, Optional[str], Optional[str]]:
    """Google's `start`/`end` → (instant, all_day, local_date, timezone).

    All-day events arrive as `{"date": "2026-08-15"}` with no time at all. An
    event on the 15th is the 15th *locally*: converting it to UTC midnight puts
    it on the 14th for anyone west of Greenwich. The local date is therefore
    kept verbatim, and the instant is only a sortable approximation.
    """
    if not value:
        return None, False, None, None

    tz_name = value.get("timeZone")

    if value.get("date"):
        raw = value["date"]
        try:
            naive = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None, True, raw, tz_name
        return naive.replace(tzinfo=timezone.utc), True, raw, tz_name

    if value.get("dateTime"):
        raw = value["dateTime"]
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None, False, None, tz_name
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed, False, None, tz_name

    return None, False, None, tz_name


def my_response_from(event: Dict[str, Any]) -> Optional[str]:
    """The user's own RSVP.

    A declined event is not on your calendar in any sense a briefing should
    mention, so this has to be captured rather than inferred from attendance.
    """
    for attendee in event.get("attendees", []) or []:
        if attendee.get("self"):
            return attendee.get("responseStatus")
    # No attendee list at all means a personal event you created.
    return "accepted" if not event.get("attendees") else None


def normalize_attendees(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for attendee in event.get("attendees", []) or []:
        out.append(
            {
                "email": attendee.get("email", ""),
                "name": attendee.get("displayName", ""),
                "response": attendee.get("responseStatus", "needsAction"),
                "optional": bool(attendee.get("optional")),
                "self": bool(attendee.get("self")),
            }
        )
    return out


def organizer_email(event: Dict[str, Any]) -> Optional[str]:
    organizer = event.get("organizer") or {}
    email = organizer.get("email")
    return email or None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

async def upsert_event(
    session: AsyncSession, *, source_id: str, calendar_id: str, raw: Dict[str, Any]
) -> Tuple[Optional[CalendarEvent], str]:
    """Returns (event, action) where action is created|updated|cancelled|skipped."""
    event_id = raw.get("id")
    if not event_id:
        return None, "skipped"

    existing = (
        await session.execute(
            select(CalendarEvent).where(
                CalendarEvent.source_id == source_id,
                CalendarEvent.source_event_id == event_id,
            )
        )
    ).scalar_one_or_none()

    # A cancelled instance arrives as a tombstone with almost no other fields.
    if raw.get("status") == "cancelled":
        if existing is not None and existing.deleted_at is None:
            existing.deleted_at = utcnow()
            existing.status = "cancelled"
            return existing, "cancelled"
        return existing, "skipped"

    starts_at, all_day, local_date, tz_name = parse_event_time(raw.get("start"))
    if starts_at is None:
        return None, "skipped"
    ends_at, _, _, _ = parse_event_time(raw.get("end"))

    updated_remote = None
    if raw.get("updated"):
        try:
            updated_remote = datetime.fromisoformat(raw["updated"].replace("Z", "+00:00"))
        except ValueError:
            updated_remote = None

    organizer_identity_id = None
    organizer = organizer_email(raw)
    if organizer:
        # Reuses Gmail's identity upsert rather than a parallel one: an
        # organizer is identified the same way a sender is — by email — and
        # having two code paths normalise and dedupe addresses differently
        # would be its own source of split identities.
        from backend.connectors.gmail.sync import upsert_identity

        identity = await upsert_identity(
            session,
            address=organizer,
            display_name=(raw.get("organizer") or {}).get("displayName", ""),
            seen_at=starts_at,
        )
        organizer_identity_id = identity.id if identity else None

    fields = dict(
        calendar_id=calendar_id,
        ical_uid=raw.get("iCalUID"),
        recurring_event_id=raw.get("recurringEventId"),
        is_instance_exception=bool(raw.get("originalStartTime")),
        title=(raw.get("summary") or "")[:1000],
        description=raw.get("description"),
        location=(raw.get("location") or "")[:1000] or None,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        local_date=local_date,
        timezone_name=tz_name,
        organizer_identity_id=organizer_identity_id,
        attendees=normalize_attendees(raw),
        my_response=my_response_from(raw),
        status=raw.get("status", "confirmed"),
        html_link=raw.get("htmlLink"),
        updated_at_remote=updated_remote,
        deleted_at=None,
    )

    # Atomic upsert, not select-then-write: a manual "check now" and the
    # worker's own poll tick can both be mid-sync for the same source at
    # once, each on its own session. Both would see no existing row for a
    # brand-new event, both would INSERT, and the second would die on the
    # (source_id, source_event_id) unique constraint — proved concretely by
    # racing two sessions through this exact function before this fix existed.
    # `existing`, captured above, is used only to label the outcome as
    # created vs. updated for the sync summary; in the narrow window where
    # two writers genuinely race, that label can end up cosmetically wrong
    # (report "created" for what became an update-via-conflict), which is
    # harmless — the stored row itself is always correct either way.
    stmt = (
        sqlite_insert(CalendarEvent)
        .values(id=new_id(), source_id=source_id, source_event_id=event_id, **fields)
        .on_conflict_do_update(
            index_elements=["source_id", "source_event_id"], set_=fields
        )
    )
    await session.execute(stmt)
    event = (
        await session.execute(
            select(CalendarEvent).where(
                CalendarEvent.source_id == source_id,
                CalendarEvent.source_event_id == event_id,
            )
        )
    ).scalar_one()
    return event, ("updated" if existing is not None else "created")


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

async def sync_calendar_once(
    session: AsyncSession,
    client: CalendarClient,
    *,
    source_id: str,
    calendar_id: str = "primary",
    past_days: int = 7,
    future_days: int = 90,
) -> CalendarSyncOutcome:
    """One pass. Window-based, with a sync token for cheap deltas inside it."""
    outcome = CalendarSyncOutcome()

    state = await session.get(SyncState, source_id)
    sync_token = (state.watermark or {}).get("sync_token") if state else None

    now = utcnow()
    time_min = (now - timedelta(days=past_days)).isoformat()
    time_max = (now + timedelta(days=future_days)).isoformat()

    try:
        items, next_token = await collect_events(
            client, calendar_id, time_min=time_min, time_max=time_max, sync_token=sync_token
        )
    except SyncTokenExpired:
        # Cheap, unlike Gmail: re-listing a bounded window is not a mailbox
        # import, so there is no need for a special recovery path.
        outcome.used_full_window = True
        items, next_token = await collect_events(
            client, calendar_id, time_min=time_min, time_max=time_max
        )
        log.info("calendar sync token expired; re-listed the window")

    for raw in items:
        try:
            _, action = await upsert_event(
                session, source_id=source_id, calendar_id=calendar_id, raw=raw
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to upsert calendar event")
            outcome.errors.append(f"{raw.get('id')}: {type(exc).__name__}: {exc}")
            continue

        if action == "created":
            outcome.created += 1
        elif action == "updated":
            outcome.updated += 1
        elif action == "cancelled":
            outcome.cancelled += 1
        else:
            outcome.skipped += 1

    if state is None:
        stmt = sqlite_insert(SyncState).values(
            source_id=source_id,
            watermark={"sync_token": next_token} if next_token else {},
            recording_since=now,
            last_attempt_at=now,
            last_success_at=now,
            updated_at=now,
        )
        await session.execute(stmt)
    else:
        # Only after every page — nextSyncToken appears on the last page only.
        if next_token:
            state.watermark = {"sync_token": next_token}
        if state.recording_since is None:
            state.recording_since = now
        state.last_attempt_at = now
        state.last_success_at = now
        state.last_error = "; ".join(outcome.errors[:3]) or None
        state.consecutive_failures = 0
        state.messages_ingested = (state.messages_ingested or 0) + outcome.created

    outcome.sync_token = next_token
    return outcome


async def upcoming_events(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    include_declined: bool = False,
) -> List[CalendarEvent]:
    """Events in a range — the query "what do I have tomorrow?" resolves to.

    An indexed range scan, which is the entire reason instances are expanded
    at ingest rather than evaluated from a recurrence rule at query time.

    This is an **overlap** test (`starts_at < end AND ends_at > start`), not a
    "starts inside the window" test. The difference matters for anything
    longer than a point event: a 3-day conference has `starts_at` on day one,
    so a query for day two of it must still match on `ends_at`, or it silently
    vanishes from every day but the first — the "what do I have tomorrow"
    answer would be wrong precisely when the answer matters most (you're
    already at the multi-day thing). An event with no parseable `ends_at`
    (Google sent something malformed or omitted it) falls back to the old
    starts-in-range test, since a duration of unknown length cannot overlap
    anything by the general rule.
    """
    stmt = (
        select(CalendarEvent)
        .where(
            CalendarEvent.deleted_at.is_(None),
            CalendarEvent.status != "cancelled",
            CalendarEvent.starts_at < end,
            (
                (CalendarEvent.ends_at.is_(None) & (CalendarEvent.starts_at >= start))
                | (CalendarEvent.ends_at.isnot(None) & (CalendarEvent.ends_at > start))
            ),
        )
        .order_by(CalendarEvent.starts_at)
    )
    if not include_declined:
        stmt = stmt.where(
            (CalendarEvent.my_response.is_(None)) | (CalendarEvent.my_response != "declined")
        )
    return list((await session.execute(stmt)).scalars().all())
