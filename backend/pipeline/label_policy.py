"""Which Gmail messages get ingested.

Default: INBOX and SENT, minus the Promotions, Social and Forums category
tabs. Those three are typically half an inbox and are the main thing that
poisons semantic search — you ask about a flight and get twelve promotional
fares.

**CATEGORY_UPDATES is deliberately NOT excluded.** Gmail files receipts,
shipping notices, bills, and appointment reminders there. That is transactional
mail with real deadlines in it, and it is exactly what the commitment
extractor exists for. Excluding it would quietly lose the bills.

A custom mode lets the user pick labels explicitly, for anyone whose filters
auto-archive things they still read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, List, Sequence, Set


class LabelMode(str, Enum):
    DEFAULT = "default"  # inbox + sent, minus noisy category tabs
    ALL_MAIL = "all_mail"  # everything except spam and trash
    CUSTOM = "custom"  # exactly the labels the user names


#: Always excluded, in every mode. Spam is hostile by definition, and Trash is
#: mail the user already discarded. Drafts are things never sent — ingesting
#: one makes Gary report a message that does not exist.
ALWAYS_EXCLUDED: Set[str] = {"SPAM", "TRASH", "DRAFT"}

DEFAULT_INCLUDE: Set[str] = {"INBOX", "SENT"}

#: Noise tabs. Updates is absent on purpose — see the module docstring.
DEFAULT_EXCLUDE: Set[str] = {
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_FORUMS",
}

KNOWN_SYSTEM_LABELS: Set[str] = {
    "INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED",
    "IMPORTANT", "CHAT",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


@dataclass
class LabelPolicy:
    mode: LabelMode = LabelMode.DEFAULT
    include: Set[str] = field(default_factory=lambda: set(DEFAULT_INCLUDE))
    exclude: Set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDE))

    @classmethod
    def from_settings(
        cls, mode: str, include_csv: str = "", exclude_csv: str = ""
    ) -> "LabelPolicy":
        parsed_mode = LabelMode(mode) if mode in {m.value for m in LabelMode} else LabelMode.DEFAULT

        def split(csv: str) -> Set[str]:
            return {p.strip().upper() for p in csv.split(",") if p.strip()}

        if parsed_mode is LabelMode.CUSTOM:
            include = split(include_csv) or set(DEFAULT_INCLUDE)
            return cls(parsed_mode, include, split(exclude_csv))
        if parsed_mode is LabelMode.ALL_MAIL:
            return cls(parsed_mode, set(), set())
        return cls(LabelMode.DEFAULT, set(DEFAULT_INCLUDE), set(DEFAULT_EXCLUDE))

    def should_ingest(self, labels: Iterable[str]) -> bool:
        """Does a message carrying these labels belong in the database?"""
        label_set = {str(item).strip().upper() for item in labels if str(item).strip()}

        if label_set & ALWAYS_EXCLUDED:
            return False
        if self.exclude and label_set & self.exclude:
            return False
        if not self.include:
            return True  # all-mail mode
        return bool(label_set & self.include)

    def reason(self, labels: Iterable[str]) -> str:
        """Why a message was kept or skipped — for the ingestion log."""
        label_set = {str(item).strip().upper() for item in labels if str(item).strip()}

        blocked = label_set & ALWAYS_EXCLUDED
        if blocked:
            return f"excluded: {', '.join(sorted(blocked))}"
        hit = label_set & self.exclude if self.exclude else set()
        if hit:
            return f"excluded category: {', '.join(sorted(hit))}"
        if not self.include:
            return "all-mail mode"
        matched = label_set & self.include
        if matched:
            return f"included via {', '.join(sorted(matched))}"
        return "no included label present"

    def gmail_query(self) -> str:
        """Server-side filter for the bounded catch-up path.

        `history.list` cannot filter by label, so ingestion filters client-side
        via `should_ingest`. This query is only for `messages.list` during a
        seed or a gap recovery, where narrowing server-side saves real quota.
        """
        parts: List[str] = []
        for label in sorted(self.include):
            parts.append(f"label:{label.lower()}")
        query = " OR ".join(parts)
        if query and len(self.include) > 1:
            query = f"({query})"
        for label in sorted(self.exclude | ALWAYS_EXCLUDED):
            query += f" -label:{label.lower()}"
        return query.strip()


def unknown_labels(names: Sequence[str], available: Sequence[str]) -> List[str]:
    """Configured labels that do not exist on the account.

    A typo in a custom label list is a silent gap — the label simply never
    matches and that mail never arrives. Surfaced on the status page instead.
    """
    have = {a.strip().upper() for a in available} | KNOWN_SYSTEM_LABELS
    return sorted({n.strip().upper() for n in names if n.strip()} - have)
