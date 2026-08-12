"""Gmail message → NormalizedMessage.

Three jobs, each with a failure mode that matters:

1. **Decode.** Old and broken mail carries mislabelled charsets and malformed
   MIME. One bad message must never abort a batch.
2. **Pick the right timestamp.** The `Date:` header is written by the sender
   and is routinely wrong. Gmail's `internalDate` is the server's own receive
   time and cannot be spoofed — that is the ordering key.
3. **Separate prose from noise.** Quoted replies and signatures dominate a
   thread by volume and say nothing new. Everything downstream — embedding,
   extraction, grounding — works on the cleaned body.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import quopri
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

#: Body text beyond this is truncated. A 25MB newsletter must not stall the
#: pipeline or bloat the row.
MAX_BODY_CHARS = 256_000

#: If the Date header disagrees with the server receive time by more than this,
#: the header is not believed.
DATE_DISTRUST_WINDOW = timedelta(days=7)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def decode_b64url(data: str) -> Tuple[bytes, bool]:
    """Gmail's base64url payloads. Returns (bytes, degraded)."""
    if not data:
        return b"", False
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded), False
    except (binascii.Error, ValueError):
        try:
            return base64.b64decode(padded, validate=False), True
        except Exception:  # noqa: BLE001
            return b"", True


def decode_text(raw: bytes, charset: Optional[str] = None) -> Tuple[str, bool]:
    """Bytes → str, never raising. Returns (text, degraded).

    `errors="replace"` rather than strict: a single bad byte in a decade-old
    message must not cost the whole message.
    """
    if not raw:
        return "", False
    candidates = [charset, "utf-8", "windows-1252", "latin-1"]
    for enc in candidates:
        if not enc:
            continue
        try:
            return raw.decode(enc), False
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), True


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_BLOCK_BREAK = re.compile(r"</(p|div|tr|h[1-6]|li|blockquote)>", re.IGNORECASE)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&mdash;": "—", "&ndash;": "–", "&hellip;": "…",
}

#: Remote resources in mail are tracking pixels. Loading one tells the sender
#: you read it, when, and roughly where from.
_REMOTE_SRC = re.compile(
    r'\s(?:src|background|poster)\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)', re.IGNORECASE
)
_STYLE_URL = re.compile(r"url\(\s*['\"]?(?:https?:)?//[^)]*\)", re.IGNORECASE)
_EVENT_HANDLER = re.compile(r'\son\w+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)', re.IGNORECASE)


def sanitize_html(html: str) -> str:
    """Strip scripts, event handlers, and every remote resource reference.

    The renderer refuses to load images anyway, but stripping at ingest means a
    tracker never reaches storage — defence at both ends, since stored HTML may
    later be viewed by something other than our own renderer.
    """
    if not html:
        return ""
    cleaned = _SCRIPT_STYLE.sub(" ", html)
    cleaned = _EVENT_HANDLER.sub("", cleaned)
    cleaned = _REMOTE_SRC.sub(' data-blocked-src="removed"', cleaned)
    cleaned = _STYLE_URL.sub("none", cleaned)
    return cleaned


def html_to_text(html: str) -> str:
    if not html:
        return ""
    text = _SCRIPT_STYLE.sub(" ", html)
    text = _BR.sub("\n", text)
    text = _BLOCK_BREAK.sub("\n\n", text)
    text = _TAG.sub(" ", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r"&#(\d+);", lambda m: _safe_chr(m.group(1)), text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _safe_chr(code: str) -> str:
    try:
        return chr(int(code))
    except (ValueError, OverflowError):
        return ""


# ---------------------------------------------------------------------------
# Quote and signature stripping
# ---------------------------------------------------------------------------

_QUOTE_MARKERS = [
    re.compile(r"^\s*On .{5,120}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE),
    re.compile(r"^\s*Get Outlook for \w+", re.IGNORECASE),
]

_SIGNATURE_MARKERS = [
    re.compile(r"^\s*--\s*$"),
    re.compile(r"^\s*—\s*$"),
    re.compile(r"^\s*Best( regards)?,?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(Kind|Warm) regards,?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(Thanks|Cheers|Sincerely|Regards),?\s*$", re.IGNORECASE),
]


def split_quoted(text: str) -> Tuple[str, str]:
    """Split into (new content, quoted remainder)."""
    if not text:
        return "", ""
    lines = text.splitlines()

    for i, line in enumerate(lines):
        if any(marker.match(line) for marker in _QUOTE_MARKERS):
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
        # A run of '>' quoting that continues to the end.
        if line.lstrip().startswith(">"):
            rest = [l for l in lines[i:] if l.strip()]
            quoted = [l for l in rest if l.lstrip().startswith(">")]
            if rest and len(quoted) / len(rest) > 0.6:
                return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()

    return text.strip(), ""


def split_signature(text: str) -> Tuple[str, str]:
    """Split into (body, signature).

    The signature is kept rather than discarded: people publish their own phone
    numbers and alternate addresses there, which is the strongest automatic
    signal for linking identities.
    """
    if not text:
        return "", ""
    lines = text.splitlines()

    # Only look near the end — "Thanks," mid-message is not a sign-off.
    search_from = max(0, len(lines) - 12)
    for i in range(search_from, len(lines)):
        if any(marker.match(lines[i]) for marker in _SIGNATURE_MARKERS):
            tail = "\n".join(lines[i:]).strip()
            if len(tail) < 500:
                return "\n".join(lines[:i]).strip(), tail
    return text.strip(), ""


def clean_body(text: str) -> Tuple[str, str, str]:
    """Returns (clean, quoted, signature)."""
    new_content, quoted = split_quoted(text)
    body, signature = split_signature(new_content)
    return body, quoted, signature


# ---------------------------------------------------------------------------
# MIME walking
# ---------------------------------------------------------------------------

@dataclass
class ParsedPart:
    text_plain: str = ""
    text_html: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    inline_images: int = 0
    degraded: bool = False


def _header_map(part: Dict[str, Any]) -> Dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in part.get("headers", [])}


def walk_parts(payload: Dict[str, Any], out: Optional[ParsedPart] = None) -> ParsedPart:
    """Depth-first walk of Gmail's MIME tree."""
    out = out or ParsedPart()
    if not payload:
        return out

    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    filename = payload.get("filename") or ""
    headers = _header_map(payload)
    disposition = headers.get("content-disposition", "").lower()

    if body.get("attachmentId"):
        is_inline = "inline" in disposition or bool(headers.get("content-id"))
        if is_inline and mime.startswith("image/"):
            out.inline_images += 1
        out.attachments.append(
            {
                "attachment_id": body["attachmentId"],
                "filename": filename,
                "mime_type": mime,
                "size_bytes": int(body.get("size") or 0),
                "inline": is_inline,
            }
        )
    elif body.get("data"):
        raw, degraded = decode_b64url(body["data"])
        out.degraded = out.degraded or degraded
        charset = _charset_from(headers.get("content-type", ""))
        text, text_degraded = decode_text(raw, charset)
        out.degraded = out.degraded or text_degraded
        if mime == "text/plain":
            out.text_plain += ("\n" if out.text_plain else "") + text
        elif mime == "text/html":
            out.text_html += text

    for child in payload.get("parts", []) or []:
        walk_parts(child, out)

    return out


def _charset_from(content_type: str) -> Optional[str]:
    match = re.search(r'charset\s*=\s*"?([\w-]+)"?', content_type or "", re.IGNORECASE)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def parse_internal_date(value: Any) -> Optional[datetime]:
    """Gmail `internalDate` is epoch milliseconds, as a string."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def parse_date_header(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_timestamps(
    internal_date: Any, date_header: str
) -> Tuple[datetime, Optional[datetime], bool]:
    """Returns (received_at, sent_at, header_distrusted).

    `received_at` always wins for ordering. It comes from the server and cannot
    be forged; the Date header is written by the sender and misconfigured
    clocks routinely produce mail dated days out.
    """
    received = parse_internal_date(internal_date)
    sent = parse_date_header(date_header)

    if received is None:
        # No server time at all — fall back to the header, then to now.
        return (sent or datetime.now(timezone.utc)), sent, sent is None

    if sent is None:
        return received, None, False

    distrusted = abs(sent - received) > DATE_DISTRUST_WINDOW
    return received, sent, distrusted


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------

def parse_addresses(value: str) -> List[Tuple[str, str]]:
    """Header value → [(display_name, address)].

    Uses stdlib `getaddresses` rather than a regex. A hand-rolled pattern gets
    address *lists* wrong in a way that silently loses recipients: given
    "me@gmail.com, Alex <alex@corp.com>", a greedy display-name group swallows
    the bare address into the next entry's name, and the first recipient
    disappears entirely. getaddresses splits on the real grammar, including
    quoted names containing commas.
    """
    if not value:
        return []
    out: List[Tuple[str, str]] = []
    seen: set = set()
    try:
        pairs = getaddresses([value])
    except Exception:  # noqa: BLE001 - malformed headers must not abort a batch
        return []

    for name, address in pairs:
        address = (address or "").strip().strip("<>")
        if not address or "@" not in address:
            continue
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(((name or "").strip().strip('"').strip(), address))
    return out


_SUBJECT_PREFIX = re.compile(
    r"^\s*((re|fwd?|fw|aw|sv|vs|antw|res)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE
)


def normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd: chains — used as a threading fallback."""
    if not subject:
        return ""
    return _SUBJECT_PREFIX.sub("", subject).strip()


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------

@dataclass
class ParsedMessage:
    source_message_id: str
    source_thread_id: str
    rfc_message_id: str
    in_reply_to: str
    references: List[str]

    from_name: str
    from_address: str
    to: List[Tuple[str, str]]
    cc: List[Tuple[str, str]]
    bcc: List[Tuple[str, str]]
    reply_to: List[Tuple[str, str]]

    subject: str
    subject_normalized: str
    body_text: str
    body_html: str
    body_clean: str
    quoted_text: str
    signature_block: str
    snippet: str

    received_at: datetime
    sent_at: Optional[datetime]
    date_header_distrusted: bool

    labels: List[str]
    headers: Dict[str, str]
    attachments: List[Dict[str, Any]]
    inline_image_count: int

    size_bytes: int
    truncated: bool
    decode_degraded: bool
    content_hash: str
    list_id: str

    @property
    def is_draft(self) -> bool:
        return "DRAFT" in self.labels

    @property
    def is_sent(self) -> bool:
        return "SENT" in self.labels

    @property
    def is_read(self) -> bool:
        return "UNREAD" not in self.labels


def parse_message(raw: Dict[str, Any]) -> ParsedMessage:
    """Parse one `messages.get(format=full)` response.

    Total, in the sense that it does not raise on malformed input — it records
    degradation and returns what it could recover. A sync of 100 messages must
    not fail because one of them is broken.
    """
    payload = raw.get("payload") or {}
    headers = _header_map(payload)
    parts = walk_parts(payload)

    body_text = parts.text_plain
    if not body_text.strip() and parts.text_html:
        body_text = html_to_text(parts.text_html)

    truncated = len(body_text) > MAX_BODY_CHARS
    if truncated:
        body_text = body_text[:MAX_BODY_CHARS]

    clean, quoted, signature = clean_body(body_text)
    received_at, sent_at, distrusted = resolve_timestamps(
        raw.get("internalDate"), headers.get("date", "")
    )

    from_pairs = parse_addresses(headers.get("from", ""))
    from_name, from_address = from_pairs[0] if from_pairs else ("", "")

    references = [r for r in (headers.get("references", "") or "").split() if r]

    return ParsedMessage(
        source_message_id=raw.get("id", ""),
        source_thread_id=raw.get("threadId", ""),
        rfc_message_id=headers.get("message-id", "").strip("<> "),
        in_reply_to=headers.get("in-reply-to", "").strip("<> "),
        references=references,
        from_name=from_name,
        from_address=from_address,
        to=parse_addresses(headers.get("to", "")),
        cc=parse_addresses(headers.get("cc", "")),
        bcc=parse_addresses(headers.get("bcc", "")),
        reply_to=parse_addresses(headers.get("reply-to", "")),
        subject=headers.get("subject", ""),
        subject_normalized=normalize_subject(headers.get("subject", "")),
        body_text=body_text,
        body_html=sanitize_html(parts.text_html),
        body_clean=clean,
        quoted_text=quoted,
        signature_block=signature,
        snippet=(raw.get("snippet") or "")[:500],
        received_at=received_at,
        sent_at=sent_at,
        date_header_distrusted=distrusted,
        labels=list(raw.get("labelIds") or []),
        headers=headers,
        attachments=[a for a in parts.attachments if not a["inline"]],
        inline_image_count=parts.inline_images,
        size_bytes=int(raw.get("sizeEstimate") or 0),
        truncated=truncated,
        decode_degraded=parts.degraded,
        content_hash=content_hash_for(
            headers.get("message-id", ""), headers.get("subject", ""), body_text
        ),
        list_id=headers.get("list-id", ""),
    )


def content_hash_for(message_id: str, subject: str, body: str) -> str:
    """Stable hash for near-duplicate detection across accounts."""
    digest = hashlib.sha256()
    digest.update((message_id or "").encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update((subject or "").encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update((body or "")[:20_000].encode("utf-8", "replace"))
    return digest.hexdigest()
