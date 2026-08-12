"""An in-memory Gmail, faithful to the behaviours that actually cause bugs.

Deliberately reproduces:
  * multi-page `history.list` with `nextPageToken`
  * a 404 when `startHistoryId` is older than the retained window
  * distinct `messagesAdded` / `messagesDeleted` / `labelsAdded` record types
  * 429 and 403 rateLimitExceeded responses
  * non-contiguous history IDs

Driven through `httpx.MockTransport`, so the real `GmailClient` — including its
retry, backoff and pagination — is what runs under test.
"""

from __future__ import annotations

import base64
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def build_raw_message(
    *,
    message_id: str,
    thread_id: str = "",
    sender: str = "Sarah Chen <sarah@example.com>",
    to: str = "me@gmail.com",
    subject: str = "Hello",
    body: str = "Just checking in about the project.",
    labels: Optional[List[str]] = None,
    received_ms: Optional[int] = None,
    html: str = "",
    attachments: Optional[List[Dict[str, Any]]] = None,
    extra_headers: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    labels = labels if labels is not None else ["INBOX", "UNREAD"]
    received_ms = received_ms or int(
        datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc).timestamp() * 1000
    )
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": f"<{message_id}@example.com>"},
        {
            "name": "Date",
            "value": datetime.fromtimestamp(received_ms / 1000, tz=timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S +0000"
            ),
        },
    ]
    headers.extend(extra_headers or [])

    parts: List[Dict[str, Any]] = [
        {
            "mimeType": "text/plain",
            "headers": [{"name": "Content-Type", "value": "text/plain; charset=utf-8"}],
            "body": {"data": b64(body), "size": len(body)},
        }
    ]
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html), "size": len(html)}})
    for att in attachments or []:
        parts.append(
            {
                "mimeType": att.get("mime_type", "application/pdf"),
                "filename": att.get("filename", "file.pdf"),
                "body": {
                    "attachmentId": att.get("attachment_id", "att1"),
                    "size": att.get("size_bytes", 1024),
                },
            }
        )

    return {
        "id": message_id,
        "threadId": thread_id or f"t_{message_id}",
        "internalDate": str(received_ms),
        "labelIds": labels,
        "snippet": body[:100],
        "sizeEstimate": len(body) + 500,
        "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts},
    }


@dataclass
class FakeGmail:
    """State plus an httpx handler."""

    messages: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: (history_id, record) in order. History IDs are non-contiguous on purpose.
    history: List[tuple] = field(default_factory=list)
    current_history_id: int = 1000
    #: History older than this is "expired" and yields 404.
    oldest_retained_history_id: int = 0
    profile_email: str = "me@gmail.com"

    page_size: int = 2
    #: Queue of status codes to return before succeeding, for retry testing.
    inject_failures: List[int] = field(default_factory=list)

    request_log: List[str] = field(default_factory=list)

    # ------------------------------------------------------------- authoring
    def _next_history_id(self) -> int:
        # Non-contiguous, like the real thing — code must never do arithmetic.
        self.current_history_id += random.randint(3, 40)
        return self.current_history_id

    def add_message(self, raw: Dict[str, Any]) -> int:
        self.messages[raw["id"]] = raw
        hid = self._next_history_id()
        self.history.append((hid, {"id": str(hid), "messagesAdded": [{"message": {"id": raw["id"]}}]}))
        return hid

    def delete_message(self, message_id: str) -> int:
        hid = self._next_history_id()
        self.history.append(
            (hid, {"id": str(hid), "messagesDeleted": [{"message": {"id": message_id}}]})
        )
        return hid

    def change_labels(self, message_id: str, added: List[str]) -> int:
        raw = self.messages.get(message_id)
        if raw is not None:
            raw["labelIds"] = sorted(set(raw.get("labelIds", [])) | set(added))
        hid = self._next_history_id()
        self.history.append(
            (
                hid,
                {
                    "id": str(hid),
                    "labelsAdded": [{"message": {"id": message_id}, "labelIds": added}],
                },
            )
        )
        return hid

    def expire_history_before(self, history_id: int) -> None:
        self.oldest_retained_history_id = history_id

    # -------------------------------------------------------------- handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.request_log.append(path)

        if self.inject_failures:
            status = self.inject_failures.pop(0)
            if status == 429:
                return httpx.Response(429, json={"error": {"message": "rate"}}, headers={"Retry-After": "0"})
            if status == 403:
                return httpx.Response(
                    403,
                    json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
                )
            return httpx.Response(status, json={"error": {"message": "boom"}})

        if path.endswith("/profile"):
            return httpx.Response(
                200,
                json={
                    "emailAddress": self.profile_email,
                    "historyId": str(self.current_history_id),
                    "messagesTotal": len(self.messages),
                },
            )

        if path.endswith("/history"):
            return self._history_response(request)

        if "/messages/" in path and "/attachments/" in path:
            return httpx.Response(200, json={"data": b64("file contents"), "size": 13})

        if "/messages/" in path:
            message_id = path.rsplit("/", 1)[-1]
            raw = self.messages.get(message_id)
            if raw is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json=raw)

        if path.endswith("/messages"):
            return self._list_response(request)

        if path.endswith("/labels"):
            return httpx.Response(
                200,
                json={"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "Work", "name": "Work"}]},
            )

        return httpx.Response(404, json={"error": {"message": f"unhandled {path}"}})

    def _history_response(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start = int(params.get("startHistoryId", "0") or 0)

        if start < self.oldest_retained_history_id:
            return httpx.Response(404, json={"error": {"message": "historyId too old"}})

        relevant = [rec for hid, rec in self.history if hid > start]
        offset = int(params.get("pageToken", "0") or 0)
        page = relevant[offset : offset + self.page_size]

        body: Dict[str, Any] = {"history": page, "historyId": str(self.current_history_id)}
        if offset + self.page_size < len(relevant):
            body["nextPageToken"] = str(offset + self.page_size)
        return httpx.Response(200, json=body)

    def _list_response(self, request: httpx.Request) -> httpx.Response:
        ids = sorted(self.messages)
        offset = int(request.url.params.get("pageToken", "0") or 0)
        size = int(request.url.params.get("maxResults", "100") or 100)
        page = ids[offset : offset + size]
        body: Dict[str, Any] = {
            "messages": [{"id": m, "threadId": self.messages[m]["threadId"]} for m in page],
            "resultSizeEstimate": len(ids),
        }
        if offset + size < len(ids):
            body["nextPageToken"] = str(offset + size)
        return httpx.Response(200, json=body)

    def client(self) -> httpx.AsyncClient:
        from backend.connectors.gmail.client import API_BASE

        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), base_url=API_BASE
        )
