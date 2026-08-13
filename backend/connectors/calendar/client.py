"""Google Calendar REST client.

Differs from Gmail in one structural way: Calendar syncs a **window**, not a
watermark. A calendar's value is in the future, and tomorrow's meeting was
created last week — "only events created from now on" would make exactly the
events you care about invisible.

`syncToken` still gives cheap deltas *within* that window. When it expires the
recovery is genuinely cheap, because re-listing a bounded window is not the
same thing as importing a mailbox.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/calendar/v3"
MAX_ATTEMPTS = 5


class CalendarError(RuntimeError):
    pass


class SyncTokenExpired(CalendarError):
    """HTTP 410. The token is too old; re-list the window for a fresh one."""


class CalendarAuthError(CalendarError):
    """401/403 that re-authorisation, not retrying, would fix."""


class CalendarClient:
    def __init__(
        self,
        access_token: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 60.0,
    ) -> None:
        self.access_token = access_token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=API_BASE, timeout=timeout)

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = await self._client.get(path, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise CalendarError(f"Could not reach Google Calendar: {exc}") from exc
                await self._backoff(attempt)
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 410:
                raise SyncTokenExpired("Calendar sync token is no longer valid.")
            if resp.status_code == 401:
                raise CalendarAuthError("Calendar rejected the access token (401).")
            if resp.status_code == 403:
                reason = _reason(resp)
                if reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
                    if attempt == MAX_ATTEMPTS - 1:
                        raise CalendarError(f"Calendar rate limit persisted: {reason}")
                    await self._backoff(attempt, resp)
                    continue
                raise CalendarAuthError(
                    f"Calendar refused the request ({reason}). If this says "
                    "'accessNotConfigured', enable the Google Calendar API for your "
                    "Cloud project."
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_ATTEMPTS - 1:
                    raise CalendarError(f"Calendar returned {resp.status_code} repeatedly.")
                await self._backoff(attempt, resp)
                continue
            raise CalendarError(f"Calendar returned {resp.status_code}: {resp.text[:300]}")

        raise CalendarError("Exhausted retries against Google Calendar.")

    async def _backoff(self, attempt: int, resp: Optional[httpx.Response] = None) -> None:
        if resp is not None and resp.headers.get("Retry-After"):
            try:
                await asyncio.sleep(min(float(resp.headers["Retry-After"]), 60.0))
                return
            except ValueError:
                pass
        await asyncio.sleep(min(2**attempt, 32) * random.uniform(0.5, 1.5))

    # ------------------------------------------------------------- endpoints
    async def list_calendars(self) -> List[Dict[str, Any]]:
        payload = await self._get("/users/me/calendarList")
        return payload.get("items", [])

    async def list_events(
        self,
        calendar_id: str = "primary",
        *,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        sync_token: Optional[str] = None,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"maxResults": 250, "showDeleted": True}
        if sync_token:
            # timeMin/timeMax are rejected alongside syncToken — the token
            # already encodes the window it was issued for.
            params["syncToken"] = sync_token
        else:
            # Expand recurring series into instances server-side, so "what do I
            # have Tuesday" stays an indexed range scan rather than an RRULE
            # evaluation with its own DST and exception bugs.
            params["singleEvents"] = True
            params["orderBy"] = "startTime"
            if time_min:
                params["timeMin"] = time_min
            if time_max:
                params["timeMax"] = time_max
        if page_token:
            params["pageToken"] = page_token

        return await self._get(f"/calendars/{_quote(calendar_id)}/events", params)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _reason(resp: httpx.Response) -> str:
    try:
        errors = resp.json().get("error", {}).get("errors", [])
        return errors[0].get("reason", "") if errors else ""
    except Exception:  # noqa: BLE001
        return ""


async def collect_events(
    client: CalendarClient,
    calendar_id: str,
    *,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    sync_token: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Walk every page, then return the next sync token.

    Same rule as Gmail history: the token comes back only after the final page.
    Storing it early would silently skip everything that followed — and
    `nextSyncToken` is only present on the last page anyway.
    """
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    next_sync: Optional[str] = None

    while True:
        payload = await client.list_events(
            calendar_id,
            time_min=time_min,
            time_max=time_max,
            sync_token=sync_token,
            page_token=page_token,
        )
        items.extend(payload.get("items", []))
        next_sync = payload.get("nextSyncToken") or next_sync
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return items, next_sync
