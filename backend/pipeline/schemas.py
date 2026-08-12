"""Contracts for the ingest pipeline's extraction step.

This is the boundary where unstructured prose becomes structured rows. Getting
it right is what makes "what's due Friday?" a `WHERE due_at BETWEEN ...` query
instead of a hopeful semantic search.

Two rules drive the design:

  1. **Everything extracted must be grounded.** Each commitment carries a
     verbatim quote from the source message. If the quote is not actually in
     the message, the extraction is a hallucination and is discarded — a real
     check, not a prompt asking nicely.

  2. **Relative dates resolve against the message, not against now.** "by
     Friday" in an email from last Tuesday means a specific date. The extractor
     is given the message timestamp and the user's timezone, returns absolute
     ISO-8601, and the result is sanity-checked against the message time.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------

class CommitmentKind(str, Enum):
    TASK = "task"  # something to do
    DEADLINE = "deadline"  # a dated obligation
    MEETING_REQUEST = "meeting_request"  # someone wants time
    QUESTION = "question"  # awaiting an answer
    PROMISE = "promise"  # someone said they would do something


class Owner(str, Enum):
    """Who has to act. Drives 'what do I owe' vs 'what am I waiting on'."""

    ME = "me"
    THEM = "them"
    UNCLEAR = "unclear"


class DuePrecision(str, Enum):
    """How precise the stated deadline is.

    "by 5pm Friday" and "sometime next week" both become timestamps, but only
    one should drive an alarm. Storing the precision keeps the UI honest.
    """

    EXACT = "exact"  # a specific time
    DAY = "day"  # a specific date
    WEEK = "week"  # "next week"
    MONTH = "month"  # "end of the month"
    VAGUE = "vague"  # "soon", "when you get a chance"
    NONE = "none"  # no deadline stated


class Category(str, Enum):
    PERSONAL = "personal"
    WORK = "work"
    SCHOOL = "school"
    FINANCE = "finance"
    TRAVEL = "travel"
    HEALTH = "health"
    SHOPPING = "shopping"
    PROMOTIONAL = "promotional"
    AUTOMATED = "automated"  # receipts, alerts, CI, no-reply
    SOCIAL = "social"
    OTHER = "other"


class CommitmentStatus(str, Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"
    SNOOZED = "snoozed"
    DISMISSED = "dismissed"  # user said "not a task"


# --------------------------------------------------------------------------
# What the model returns
# --------------------------------------------------------------------------

class ExtractedCommitment(BaseModel):
    """One actionable item found in a message."""

    kind: CommitmentKind
    title: str = Field(
        max_length=200,
        description="Short imperative phrase, e.g. 'Submit research proposal'.",
    )
    owner: Owner = Owner.UNCLEAR
    due_at: Optional[datetime] = Field(
        default=None, description="Absolute ISO-8601. Null when no date is stated."
    )
    due_precision: DuePrecision = DuePrecision.NONE
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = Field(
        max_length=500,
        description="Verbatim span from the message that states this. Never paraphrase.",
    )

    @field_validator("title", "evidence_quote")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("due_at")
    @classmethod
    def _tz_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        # A naive datetime silently compares wrong against tz-aware rows.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class CommitmentUpdate(BaseModel):
    """A change to a commitment already open in this thread.

    Without this, a five-reply thread about one deadline produces five
    duplicate tasks, and "actually, let's move it to Monday" creates a second
    task rather than moving the first.
    """

    commitment_id: str
    new_status: Optional[CommitmentStatus] = None
    new_due_at: Optional[datetime] = None
    reason: str = Field(default="", max_length=300)
    evidence_quote: str = Field(default="", max_length=500)

    @field_validator("new_due_at")
    @classmethod
    def _tz_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class MessageAnalysis(BaseModel):
    """The complete structured output for one message or conversation session.

    This is the JSON schema handed to the fast model via Ollama's `format=`.
    """

    category: Category = Category.OTHER
    importance: float = Field(ge=0.0, le=1.0, default=0.0)
    requires_action: bool = False
    is_automated: bool = Field(
        default=False,
        description="Machine-generated: receipts, notifications, newsletters, no-reply.",
    )
    summary: str = Field(default="", max_length=300)
    people: List[str] = Field(default_factory=list)
    commitments: List[ExtractedCommitment] = Field(default_factory=list)
    updates: List[CommitmentUpdate] = Field(default_factory=list)

    @field_validator("people")
    @classmethod
    def _dedupe_people(cls, v: List[str]) -> List[str]:
        seen: List[str] = []
        for name in v:
            cleaned = name.strip()
            if cleaned and cleaned.lower() not in {s.lower() for s in seen}:
                seen.append(cleaned)
        return seen


# --------------------------------------------------------------------------
# Grounding — the hallucination check
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Fold whitespace, case, and unicode punctuation for comparison.

    Models routinely return smart quotes where the source had straight ones,
    or collapse a line break into a space. Neither is a hallucination.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return _WS.sub(" ", text).strip().lower()


def _token_overlap(quote: str, source: str) -> float:
    """Fraction of the quote's tokens present in the source."""
    q_tokens = [t for t in _normalise(quote).split(" ") if t]
    if not q_tokens:
        return 0.0
    s_tokens = set(_normalise(source).split(" "))
    return sum(1 for t in q_tokens if t in s_tokens) / len(q_tokens)


class GroundingResult(BaseModel):
    grounded: bool
    method: Literal["exact", "fuzzy", "none"]
    overlap: float
    reason: str = ""


def verify_grounding(
    quote: str, source_text: str, *, min_overlap: float = 0.8, min_tokens: int = 3
) -> GroundingResult:
    """Is this quote actually present in the message?

    Exact (normalised) substring is the happy path. Token overlap is the
    fallback for minor rewording. Anything below the threshold is treated as
    fabricated.
    """
    if not quote or not quote.strip():
        return GroundingResult(
            grounded=False, method="none", overlap=0.0, reason="empty quote"
        )
    if not source_text or not source_text.strip():
        return GroundingResult(
            grounded=False, method="none", overlap=0.0, reason="empty source"
        )

    # Length gate applies to *both* paths. A one-word quote like "due" is a
    # substring of half the emails ever written; matching it verifies nothing.
    tokens = [t for t in _normalise(quote).split(" ") if t]
    if len(tokens) < min_tokens:
        return GroundingResult(
            grounded=False,
            method="none",
            overlap=0.0,
            reason=f"quote too short to verify ({len(tokens)} tokens)",
        )

    if _normalise(quote) in _normalise(source_text):
        return GroundingResult(grounded=True, method="exact", overlap=1.0)

    overlap = _token_overlap(quote, source_text)
    if overlap >= min_overlap:
        return GroundingResult(grounded=True, method="fuzzy", overlap=round(overlap, 3))

    return GroundingResult(
        grounded=False,
        method="none",
        overlap=round(overlap, 3),
        reason=f"only {overlap:.0%} of the quote appears in the message",
    )


# --------------------------------------------------------------------------
# Date sanity
# --------------------------------------------------------------------------

#: A deadline this far before the message was sent is a resolution error, not
#: a real retroactive deadline.
BACKDATE_GRACE = timedelta(days=1)

#: Nobody emails you about something 5+ years out; that is a parse artefact
#: (commonly a year hallucinated as 2027 when the source said "the 27th").
MAX_HORIZON = timedelta(days=365 * 5)


class DateCheck(BaseModel):
    ok: bool
    reason: str = ""
    downgrade_to: Optional[DuePrecision] = None


def check_due_date(due_at: Optional[datetime], message_time: datetime) -> DateCheck:
    """Reject deadlines that cannot be right relative to the message."""
    if due_at is None:
        return DateCheck(ok=True)

    if message_time.tzinfo is None:
        message_time = message_time.replace(tzinfo=timezone.utc)

    if due_at < message_time - BACKDATE_GRACE:
        return DateCheck(
            ok=False,
            reason=f"deadline {due_at.isoformat()} precedes the message {message_time.isoformat()}",
            downgrade_to=DuePrecision.VAGUE,
        )

    if due_at > message_time + MAX_HORIZON:
        return DateCheck(
            ok=False,
            reason=f"deadline {due_at.isoformat()} is implausibly far out",
            downgrade_to=DuePrecision.VAGUE,
        )

    return DateCheck(ok=True)


# --------------------------------------------------------------------------
# Post-processing
# --------------------------------------------------------------------------

class SanitisedAnalysis(BaseModel):
    """What survives validation, plus what was thrown away and why."""

    analysis: MessageAnalysis
    dropped: List[str] = Field(default_factory=list)


def sanitise(
    analysis: MessageAnalysis,
    *,
    source_text: str,
    message_time: datetime,
    min_confidence: float = 0.35,
) -> SanitisedAnalysis:
    """Enforce grounding, date sanity, and a confidence floor.

    Runs on every extraction before anything reaches the database. A model that
    invents a deadline gets its invention dropped here rather than producing a
    phantom alert at 9am.
    """
    kept: List[ExtractedCommitment] = []
    dropped: List[str] = []

    for c in analysis.commitments:
        if c.confidence < min_confidence:
            dropped.append(f"{c.title!r}: confidence {c.confidence:.2f} below floor")
            continue

        grounding = verify_grounding(c.evidence_quote, source_text)
        if not grounding.grounded:
            dropped.append(f"{c.title!r}: ungrounded — {grounding.reason}")
            continue

        date_check = check_due_date(c.due_at, message_time)
        if not date_check.ok:
            # Keep the item, lose the untrustworthy date: "there is something
            # to do here" is still useful; a wrong date is worse than none.
            dropped.append(f"{c.title!r}: dropped date — {date_check.reason}")
            c = c.model_copy(
                update={
                    "due_at": None,
                    "due_precision": date_check.downgrade_to or DuePrecision.NONE,
                }
            )

        kept.append(c)

    kept_updates: List[CommitmentUpdate] = []
    for u in analysis.updates:
        if u.evidence_quote and not verify_grounding(u.evidence_quote, source_text).grounded:
            dropped.append(f"update {u.commitment_id}: ungrounded")
            continue
        kept_updates.append(u)

    # An automated message asserting it needs action is almost always a
    # marketing "act now" — do not let it drive notifications.
    requires_action = analysis.requires_action and not analysis.is_automated

    return SanitisedAnalysis(
        analysis=analysis.model_copy(
            update={
                "commitments": kept,
                "updates": kept_updates,
                "requires_action": requires_action,
            }
        ),
        dropped=dropped,
    )


def analysis_json_schema() -> dict:
    """JSON schema handed to Ollama's structured-output `format=` parameter."""
    return MessageAnalysis.model_json_schema()
