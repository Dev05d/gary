"""Rule-based triage: decide what needs the LLM before spending GPU on it.

Running full extraction on every message costs ~3s. At 100 messages/day that is
five minutes; at 500 it is twenty-five. Most of that is spent on newsletters
and receipts that contain nothing to extract.

Cheap deterministic checks skip the LLM for obvious bulk, cutting calls by
roughly 60–80%. The rule that keeps this honest:

    Bulk mail is only skipped when it ALSO carries no date or action language.

"Your electricity bill is due on the 15th" comes from a no-reply address and is
a genuine deadline. Sender reputation alone is the wrong signal; sender
reputation *plus* an absence of commitment language is a safe one.

Because rules will occasionally be wrong, a deterministic sample of skipped
messages is re-run through the LLM on a schedule and compared. The audit
measures what the rules actually miss instead of assuming they are fine.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from backend.pipeline.identity import is_bulk_sender, is_role_account


class TriageDecision(str, Enum):
    EXTRACT = "extract"  # run the full LLM pipeline
    CLASSIFY_ONLY = "classify_only"  # cheap category, no commitment extraction
    SKIP = "skip"  # rules alone are enough


# --------------------------------------------------------------------------
# Language cues
# --------------------------------------------------------------------------

#: Deadline-flavoured language: something is actually *owed* by a time.
#: These justify an LLM call even in a mass mailing, because bills and expiry
#: notices carry them.
_STRONG_TEMPORAL_PATTERNS = [
    r"\bdue\b", r"\bdeadline\b", r"\bexpires?\b", r"\bexpiring\b",
    r"\boverdue\b", r"\bno later than\b", r"\bcut[- ]?off\b",
    r"\bby (?:the )?(?:end of |close of )?(?:today|tomorrow|\w+day|\d{1,2})",
    r"\bbefore \w+day\b", r"\brsvp\b", r"\blast day\b",
    r"\bfinal (?:notice|reminder)\b",
]

#: Bare time references. Real signal in a message addressed to you, but far
#: too common in newsletters ("top stories this week") to justify a call on
#: their own.
_WEAK_TEMPORAL_PATTERNS = [
    r"\btoday\b", r"\btomorrow\b", r"\btonight\b",
    r"\bthis (?:week|month|morning|afternoon|evening)\b",
    r"\bnext (?:week|month|monday|tuesday|wednesday|thursday|friday)\b",
    r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b",
    r"\b\d{1,2}(?:st|nd|rd|th)\b",
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b",
    r"\b\d{1,2} (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    r"\b\d{1,2}\s?(?:am|pm)\b",
    r"\bremind(?:er)?\b", r"\bschedul(?:e|ed|ing)\b", r"\bappointment\b",
]

#: Words that signal something is being asked of the reader.
_ACTION_PATTERNS = [
    r"\bplease\b", r"\bcan you\b", r"\bcould you\b", r"\bwould you\b",
    r"\bneed(?:s)? (?:you|your)\b", r"\brequired?\b",
    r"\baction (?:is )?(?:needed|required)\b",
    r"\bconfirm\b", r"\brespond\b", r"\breply\b", r"\bsubmit\b",
    r"\bsend (?:me|us|over)\b",
    r"\bsign\b", r"\breview\b", r"\bapprove\b", r"\bcomplete\b",
    r"\bfill (?:in|out)\b",
    r"\blet me know\b", r"\bget back to me\b", r"\bfollow(?:ing)? up\b",
    r"\bawaiting\b", r"\boutstanding\b", r"\bpay(?:ment)?\b", r"\binvoice\b",
    r"\bregister\b", r"\bapply\b", r"\bdeliver\b",
]

_STRONG_TEMPORAL_RE = re.compile("|".join(_STRONG_TEMPORAL_PATTERNS), re.IGNORECASE)
_WEAK_TEMPORAL_RE = re.compile("|".join(_WEAK_TEMPORAL_PATTERNS), re.IGNORECASE)
_ACTION_RE = re.compile("|".join(_ACTION_PATTERNS), re.IGNORECASE)

#: Marketing urgency that mimics a deadline. Present in a bulk message, these
#: alone are not enough to justify extraction.
_MARKETING_RE = re.compile(
    r"\b(?:unsubscribe|shop now|limited time|act now|don'?t miss|sale ends|"
    r"% off|free shipping|last chance|exclusive offer|deal of the)\b",
    re.IGNORECASE,
)


def has_strong_temporal_language(text: str) -> bool:
    """Something is owed by a time — a deadline, not just a date."""
    return bool(text and _STRONG_TEMPORAL_RE.search(text))


def has_temporal_language(text: str) -> bool:
    """Any time reference at all, strong or weak."""
    if not text:
        return False
    return bool(_STRONG_TEMPORAL_RE.search(text) or _WEAK_TEMPORAL_RE.search(text))


def has_action_language(text: str) -> bool:
    return bool(text and _ACTION_RE.search(text))


def marketing_score(text: str) -> int:
    return len(_MARKETING_RE.findall(text or ""))


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------

@dataclass
class TriageInput:
    subject: str = ""
    body_clean: str = ""
    sender_email: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    is_from_me: bool = False
    #: Does this thread already have open commitments? If so the message may
    #: close or move one, which is worth an LLM call regardless of its own
    #: language.
    thread_has_open_commitments: bool = False


@dataclass
class TriageResult:
    decision: TriageDecision
    reasons: List[str] = field(default_factory=list)
    #: Cheap signals worth persisting even when the LLM is skipped.
    is_bulk: bool = False
    is_role_sender: bool = False

    @property
    def needs_llm(self) -> bool:
        return self.decision is not TriageDecision.SKIP


#: Bodies shorter than this carry too little to extract from, unless they are
#: replying into a thread that has something open.
MIN_BODY_CHARS = 25


def triage(message: TriageInput) -> TriageResult:
    """Decide how much processing a message deserves."""
    text = f"{message.subject}\n{message.body_clean}".strip()
    bulk = is_bulk_sender(message.headers)
    role = is_role_account(message.sender_email)
    reasons: List[str] = []

    temporal = has_temporal_language(text)
    action = has_action_language(text)

    # A reply into a thread with open items may close or reschedule one, and
    # "done!" carries no temporal or action language at all.
    if message.thread_has_open_commitments:
        return TriageResult(
            TriageDecision.EXTRACT,
            ["thread has open commitments — may update them"],
            is_bulk=bulk,
            is_role_sender=role,
        )

    if len(text) < MIN_BODY_CHARS and not temporal:
        return TriageResult(
            TriageDecision.CLASSIFY_ONLY,
            [f"body under {MIN_BODY_CHARS} chars with no date language"],
            is_bulk=bulk,
            is_role_sender=role,
        )

    # Mass mailing. `List-Unsubscribe` / `Precedence: bulk` means this went to
    # a list, so the bar for spending a GPU call is high: only a real deadline,
    # or a request that does not read as marketing.
    if bulk:
        reasons.append("bulk headers")
        strong = has_strong_temporal_language(text)
        marketing = marketing_score(text)

        if strong:
            return TriageResult(
                TriageDecision.EXTRACT,
                reasons + ["deadline language ('due', 'expires') present"],
                is_bulk=True,
                is_role_sender=role,
            )
        if action and marketing < 2:
            return TriageResult(
                TriageDecision.EXTRACT,
                reasons + ["a request, and it does not read as marketing"],
                is_bulk=True,
                is_role_sender=role,
            )
        return TriageResult(
            TriageDecision.CLASSIFY_ONLY,
            reasons
            + (
                [f"reads as marketing ({marketing} cues), no real deadline"]
                if marketing
                else ["no deadline language"]
            ),
            is_bulk=True,
            is_role_sender=role,
        )

    # Transactional mail: a role account writing to *you*, not to a list.
    # Appointment reminders, receipts, shipping notices, security alerts. A
    # plain date is enough here — "your appointment is Tuesday at 2pm" has no
    # deadline word but is exactly what you want captured.
    if role:
        reasons.append("role-account sender, not a mass mailing")
        if temporal or action:
            reasons.append("date language" if temporal else "action language")
            return TriageResult(
                TriageDecision.EXTRACT, reasons, is_bulk=False, is_role_sender=True
            )
        return TriageResult(
            TriageDecision.CLASSIFY_ONLY,
            reasons + ["no date or action language"],
            is_bulk=False,
            is_role_sender=True,
        )

    # Human mail: always worth extracting, including your own sent mail —
    # "I'll send the report Friday" is a commitment you made.
    reasons.append("sent by me" if message.is_from_me else "human sender")
    return TriageResult(
        TriageDecision.EXTRACT, reasons, is_bulk=bulk, is_role_sender=role
    )


# --------------------------------------------------------------------------
# Audit sampling
# --------------------------------------------------------------------------

DEFAULT_AUDIT_RATE = 0.05


def should_audit(
    message_id: str, rate: float = DEFAULT_AUDIT_RATE, *, salt: str = "gary-audit"
) -> bool:
    """Deterministically sample skipped messages for LLM re-checking.

    Hash-based rather than random so a given message always makes the same
    decision — re-running the audit reproduces the same sample, which is what
    makes before/after comparison meaningful.
    """
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = hashlib.sha256(f"{salt}:{message_id}".encode()).digest()
    bucket = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return bucket < rate


@dataclass
class AuditFinding:
    """One disagreement between the rules and the LLM."""

    message_id: str
    rule_decision: TriageDecision
    llm_found_commitments: int
    missed: bool  # rules skipped it, LLM found something real


@dataclass
class AuditReport:
    sampled: int = 0
    disagreements: int = 0
    findings: List[AuditFinding] = field(default_factory=list)

    @property
    def miss_rate(self) -> float:
        return round(self.disagreements / self.sampled, 4) if self.sampled else 0.0

    def summary(self) -> str:
        if not self.sampled:
            return "No messages sampled."
        if not self.disagreements:
            return f"Audited {self.sampled} skipped messages; the rules missed nothing."
        return (
            f"Audited {self.sampled} skipped messages; {self.disagreements} contained "
            f"commitments the rules skipped ({self.miss_rate:.1%}). "
            "Consider loosening triage or adding a keyword."
        )


def evaluate_audit(findings: List[AuditFinding], sampled: int) -> AuditReport:
    missed = [f for f in findings if f.missed]
    return AuditReport(sampled=sampled, disagreements=len(missed), findings=missed)


#: Miss rate above this means the rules are too aggressive and should be
#: loosened — surfaced on the status page rather than silently tolerated.
AUDIT_MISS_RATE_ALARM = 0.02


def audit_needs_attention(report: AuditReport) -> Optional[str]:
    if report.sampled < 20:
        return None  # too small a sample to conclude anything
    if report.miss_rate > AUDIT_MISS_RATE_ALARM:
        return (
            f"Triage is skipping real commitments ({report.miss_rate:.1%} of audited "
            f"messages, threshold {AUDIT_MISS_RATE_ALARM:.0%}). Review the rules."
        )
    return None
