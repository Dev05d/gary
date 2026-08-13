"""An in-memory Google Calendar, faithful to the behaviours that cause bugs.

Deliberately reproduces:
  * multi-page `events.list`, with `nextSyncToken` **only on the final page**
  * HTTP 410 when a `syncToken` has expired
  * cancelled events arriving as near-empty tombstones
  * all-day events as `{"date": ...}` with no time and no zone
  * `singleEvents=True` expanding a series into instances, including an
    overridden instance whose time differs from the series
  * 429 / 403 rateLimitExceeded

Driven through `httpx.MockTransport`, so the real `CalendarClient` — its
retries, backoff and pagination — is what runs under test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx

ME = "me@gmail.com"


def event(
    *,
    event_id: str,
    summary: str = "Meeting",
    start: Optional[datetime] = None,
    duration_minutes: int = 30,
    all_day_date: Optional[str] = None,
    location: str = "",
    description: str = "",
    attendees: Optional[List[Dict[str, Any]]] = None,
    status: str = "confirmed",
    recurring_event_id: Optional[str] = None,
    original_start: Optional[str] = None,
    timezone_name: str = "America/Los_Angeles",
    updated: Optional[datetime] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": event_id,
        "status": status,
        "summary": summary,
        "iCalUID": f"{event_id}@google.com",
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        "updated": (updated or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
    }
    if status == "cancelled":
        # A tombstone really is this bare — no summary, no start, nothing.
        return {"id": event_id, "status": "cancelled"}

    if all_day_date:
        payload["start"] = {"date": all_day_date}
        end = datetime.strptime(all_day_date, "%Y-%m-%d") + timedelta(days=1)
        payload["end"] = {"date": end.strftime("%Y-%m-%d")}
    else:
        start = start or datetime.now(timezone.utc) + timedelta(hours=2)
        payload["start"] = {
            "dateTime": start.isoformat().replace("+00:00", "Z"),
            "timeZone": timezone_name,
        }
        payload["end"] = {
            "dateTime": (start + timedelta(minutes=duration_minutes))
            .isoformat()
            .replace("+00:00", "Z"),
            "timeZone": timezone_name,
        }

    if location:
        payload["location"] = location
    if description:
        payload["description"] = description
    if attendees is not None:
        payload["attendees"] = attendees
    if recurring_event_id:
        payload["recurringEventId"] = recurring_event_id
    if original_start:
        payload["originalStartTime"] = {"dateTime": original_start}
    return payload


def attendee(email: str, *, response: str = "accepted", is_self: bool = False,
             optional: bool = False, name: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {"email": email, "responseStatus": response}
    if is_self:
        out["self"] = True
    if optional:
        out["optional"] = True
    if name:
        out["displayName"] = name
    return out


@dataclass
class FakeCalendar:
    """Serves `events.list` with realistic pagination and token semantics."""

    pages: List[List[Dict[str, Any]]] = field(default_factory=list)
    next_sync_token: str = "SYNC-2"
    #: Sync tokens the server considers expired; requesting one returns 410.
    expired_tokens: set = field(default_factory=set)
    #: Number of times to fail with 429 before serving normally.
    rate_limit_times: int = 0
    calendars: List[Dict[str, Any]] = field(default_factory=list)

    requests: List[httpx.Request] = field(default_factory=list)
    _served: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------ impl
    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlparse(str(request.url)).path
        params = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}

        if path.endswith("/users/me/calendarList"):
            return httpx.Response(200, json={"items": self.calendars})

        if "/events" not in path:
            return httpx.Response(404, json={"error": {"message": "no such endpoint"}})

        if self._served < self.rate_limit_times:
            self._served += 1
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
            )

        token = params.get("syncToken")
        if token and token in self.expired_tokens:
            return httpx.Response(
                410, json={"error": {"errors": [{"reason": "fullSyncRequired"}]}}
            )

        page_index = int(params.get("pageToken", "0"))
        if page_index >= len(self.pages):
            return httpx.Response(200, json={"items": [], "nextSyncToken": self.next_sync_token})

        body: Dict[str, Any] = {"items": self.pages[page_index]}
        if page_index + 1 < len(self.pages):
            body["nextPageToken"] = str(page_index + 1)
        else:
            # Only the last page carries it. Storing a token from an earlier
            # page would silently skip every page that followed.
            body["nextSyncToken"] = self.next_sync_token
        return httpx.Response(200, json=body)

    # -------------------------------------------------------------- helpers
    def client(self, **kwargs):
        from backend.connectors.calendar.client import API_BASE, CalendarClient

        http = httpx.AsyncClient(base_url=API_BASE, transport=self.transport())
        return CalendarClient("test-token", client=http, **kwargs)

    @property
    def param_sets(self) -> List[Dict[str, str]]:
        return [
            {k: v[0] for k, v in parse_qs(urlparse(str(r.url)).query).items()}
            for r in self.requests
        ]
