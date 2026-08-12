"""Gmail REST client.

Wraps only what the connector needs, with the operational behaviour the API
actually demands:

* **Rate limiting.** A token bucket sized to Gmail's per-user quota. A gap
  recovery burst is exactly when several sources are most likely to sync at
  once, so the limiter matters most when things are already going wrong.
* **Backoff with jitter** on 429 and 403 `rateLimitExceeded`, distinguished
  from permanent 403s like `accessNotConfigured`, which retrying never fixes.
* **A typed 404 on history expiry**, because that specific case needs a
  bounded catch-up rather than a retry.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://gmail.googleapis.com/gmail/v1"

#: Quota units. history.list is 2, messages.list is 5, messages.get is 5.
#: Worth knowing because the delta path is deliberately the cheap one.
COST_HISTORY_LIST = 2
COST_MESSAGES_LIST = 5
COST_MESSAGES_GET = 5


class GmailError(RuntimeError):
    pass


class HistoryExpired(GmailError):
    """`startHistoryId` is outside the available range (HTTP 404).

    Google guarantees history for "typically at least a week" and warns it can
    be as little as a few hours. The documented remedy is a full sync, which
    would import the entire mailbox — the opposite of live-only ingestion. The
    connector bridges the gap with a date-bounded query instead.
    """


class GmailAuthError(GmailError):
    """401/403 that re-authorisation, not retrying, would fix."""


@dataclass
class TokenBucket:
    """Quota limiter.

    Gmail budgets per user per minute. Refilling continuously rather than in
    steps avoids the thundering herd a minute boundary would create.
    """

    capacity: float = 6000.0
    refill_per_second: float = 100.0
    tokens: float = field(default=6000.0)
    updated: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def take(self, cost: float) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated) * self.refill_per_second
                )
                self.updated = now
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                shortfall = cost - self.tokens
                await asyncio.sleep(min(shortfall / self.refill_per_second, 5.0))


MAX_ATTEMPTS = 5


class GmailClient:
    def __init__(
        self,
        access_token: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        bucket: Optional[TokenBucket] = None,
        timeout: float = 60.0,
    ) -> None:
        self.access_token = access_token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=API_BASE, timeout=timeout)
        self._bucket = bucket or TokenBucket()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None, *, cost: int = 5
    ) -> Dict[str, Any]:
        await self._bucket.take(cost)

        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = await self._client.get(path, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise GmailError(f"Could not reach Gmail: {exc}") from exc
                await self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 404:
                # Only history.list uses 404 to mean "watermark too old".
                if "history" in path:
                    raise HistoryExpired(
                        "Gmail history is no longer available from that point."
                    )
                raise GmailError(f"Gmail returned 404 for {path}")

            if resp.status_code == 401:
                raise GmailAuthError("Gmail rejected the access token (401).")

            if resp.status_code == 403:
                reason = _error_reason(resp)
                # Transient throttling versus a permanently misconfigured project.
                if reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
                    if attempt == MAX_ATTEMPTS - 1:
                        raise GmailError(f"Gmail rate limit persisted: {reason}")
                    await self._sleep_backoff(attempt, resp)
                    continue
                raise GmailAuthError(
                    f"Gmail refused the request ({reason}). If this says "
                    "'accessNotConfigured', enable the Gmail API for your Google "
                    "Cloud project."
                )

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_ATTEMPTS - 1:
                    raise GmailError(f"Gmail returned {resp.status_code} repeatedly.")
                await self._sleep_backoff(attempt, resp)
                continue

            raise GmailError(f"Gmail returned {resp.status_code}: {resp.text[:300]}")

        raise GmailError("Exhausted retries against Gmail.")

    async def _sleep_backoff(self, attempt: int, resp: Optional[httpx.Response] = None) -> None:
        if resp is not None and resp.headers.get("Retry-After"):
            try:
                await asyncio.sleep(min(float(resp.headers["Retry-After"]), 60.0))
                return
            except ValueError:
                pass
        # Full jitter: several sources recovering together must not retry in
        # lockstep, which is precisely when they all hit the limit again.
        delay = min(2**attempt, 32) * random.uniform(0.5, 1.5)
        await asyncio.sleep(delay)

    # -------------------------------------------------------------- endpoints
    async def get_profile(self) -> Dict[str, Any]:
        """Current `historyId` and address. This is how a watermark is born."""
        return await self._get("/users/me/profile", cost=1)

    async def list_history(
        self,
        start_history_id: str,
        *,
        page_token: Optional[str] = None,
        label_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "startHistoryId": start_history_id,
            "maxResults": 500,
            "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
        }
        if page_token:
            params["pageToken"] = page_token
        if label_id:
            params["labelId"] = label_id
        return await self._get("/users/me/history", params, cost=COST_HISTORY_LIST)

    async def list_messages(
        self,
        *,
        query: str = "",
        page_token: Optional[str] = None,
        max_results: int = 100,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"maxResults": min(max_results, 500)}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        return await self._get("/users/me/messages", params, cost=COST_MESSAGES_LIST)

    async def get_message(self, message_id: str, *, fmt: str = "full") -> Dict[str, Any]:
        return await self._get(
            f"/users/me/messages/{message_id}", {"format": fmt}, cost=COST_MESSAGES_GET
        )

    async def get_attachment(self, message_id: str, attachment_id: str) -> Dict[str, Any]:
        return await self._get(
            f"/users/me/messages/{message_id}/attachments/{attachment_id}",
            cost=COST_MESSAGES_GET,
        )

    async def list_labels(self) -> List[Dict[str, Any]]:
        payload = await self._get("/users/me/labels", cost=1)
        return payload.get("labels", [])

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _error_reason(resp: httpx.Response) -> str:
    try:
        errors = resp.json().get("error", {}).get("errors", [])
        if errors:
            return errors[0].get("reason", "")
        return resp.json().get("error", {}).get("status", "")
    except Exception:  # noqa: BLE001
        return ""


async def collect_history(
    client: GmailClient, start_history_id: str, *, label_id: Optional[str] = None
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Walk every page of history, then return the new watermark.

    The watermark is returned rather than applied, and only after the final
    page. Advancing it per page is a silent, permanent data-loss bug: a crash
    or an early return skips everything on the remaining pages and nothing
    ever reports an error.
    """
    records: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    latest: Optional[str] = None

    while True:
        payload = await client.list_history(
            start_history_id, page_token=page_token, label_id=label_id
        )
        records.extend(payload.get("history", []))
        latest = payload.get("historyId") or latest
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return records, latest
