"""Identity and person resolution.

The same human appears as `prof.smith@university.edu`, `jsmith@gmail.com`,
`+1 555 0123`, "John Smith", "J. Smith", and "Prof Smith". Answering "what did
Professor Smith send me?" requires knowing those are one person — and *not*
merging the two unrelated John Smiths in your contacts.

Three-layer model, and the layering is the whole design:

    identities    raw handles. Never merged, never deleted. Messages point here.
    persons       resolved humans. A view over identities, not a replacement.
    identity_links  many-to-many, with confidence, method, and evidence.

**Messages foreign-key to `identity_id`, never `person_id`.** Re-resolving
identities later must not rewrite a single message row. A person is an opinion
about identities; a message's sender is a fact.

**Merges are never destructive.** A link can be added or withdrawn, and the
history survives either way. Bad merges are common and undoing one must not
require reconstructing data.

Resolution follows the standard deterministic-then-probabilistic cascade:
exact identifiers first, fuzzy signals second, with confidence bands that
route uncertain matches to the user instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Set


class IdentityKind(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    APPLE_ID = "apple_id"
    HANDLE = "handle"  # discord, slack, ...


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

#: Providers where dots in the local part are genuinely insignificant.
#: This is NOT universal — most mail servers treat `f.oo@` and `foo@` as
#: different mailboxes, and normalising them everywhere merges strangers.
_DOT_INSENSITIVE_DOMAINS = {"gmail.com", "googlemail.com"}

#: Providers known to support plus-addressing. Elsewhere a `+` may be a
#: literal character in the mailbox name.
_PLUS_ADDRESSING_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "fastmail.com",
    "protonmail.com",
    "proton.me",
    "icloud.com",
    "me.com",
    "yahoo.com",
}

_DOMAIN_ALIASES = {
    "googlemail.com": "gmail.com",
    "me.com": "icloud.com",
    "mac.com": "icloud.com",
}


def normalize_email(raw: str) -> str:
    """Canonical form for matching. The raw value is always kept alongside.

    Deliberately conservative: provider-specific rules apply only to providers
    known to implement them.
    """
    if not raw:
        return ""
    value = raw.strip().strip("<>").lower()
    if "@" not in value:
        return value

    local, _, domain = value.rpartition("@")
    domain = _DOMAIN_ALIASES.get(domain, domain)

    if domain in _PLUS_ADDRESSING_DOMAINS and "+" in local:
        local = local.split("+", 1)[0]
    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")

    return f"{local}@{domain}"


_NON_DIGIT = re.compile(r"[^\d+]")


def normalize_phone(raw: str, default_region_code: str = "1") -> Optional[str]:
    """Best-effort E.164.

    Without a full libphonenumber metadata set this cannot be exact for every
    country. It handles the common cases and *refuses* rather than guessing on
    ambiguous input — a wrong normalisation silently merges two people, which
    is far worse than leaving them separate.
    """
    if not raw:
        return None
    cleaned = _NON_DIGIT.sub("", raw.strip())
    if not cleaned:
        return None

    if cleaned.startswith("+"):
        digits = cleaned[1:]
        return f"+{digits}" if 7 <= len(digits) <= 15 else None

    if cleaned.startswith("00"):
        digits = cleaned[2:]
        return f"+{digits}" if 7 <= len(digits) <= 15 else None

    # North American Numbering Plan, the only region we infer without a prefix.
    if default_region_code == "1":
        if len(cleaned) == 10:
            return f"+1{cleaned}"
        if len(cleaned) == 11 and cleaned.startswith("1"):
            return f"+{cleaned}"

    # Anything else needs an explicit country code; guessing merges strangers.
    return None


def normalize_handle(raw: str) -> str:
    """iMessage handles are either an email or a phone number."""
    if not raw:
        return ""
    value = raw.strip()
    if "@" in value:
        return normalize_email(value)
    return normalize_phone(value) or value.lower()


# ---------------------------------------------------------------------------
# Role accounts — the things that are not people
# ---------------------------------------------------------------------------

_ROLE_LOCAL_PARTS: Set[str] = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
    "notification", "alerts", "alert", "support", "help", "helpdesk", "info",
    "contact", "admin", "administrator", "billing", "invoices", "receipts",
    "sales", "marketing", "newsletter", "news", "updates", "mailer-daemon",
    "postmaster", "bounce", "bounces", "root", "webmaster", "abuse",
    "security", "team", "hello", "hi", "careers", "jobs", "recruiting",
    "automated", "system", "notify", "reply", "mail", "email", "service",
}

_ROLE_SUBSTRINGS = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon")


def is_role_account(email: str) -> bool:
    """Is this a mailbox rather than a person?

    Without this, "who emails me most?" answers "noreply@github.com" and the
    contact list fills with brands. Role accounts still get identities — they
    just never become persons.
    """
    if not email or "@" not in email:
        return False
    local = normalize_email(email).split("@", 1)[0]

    # VERP / bounce addresses encode a per-recipient token after a separator
    # (bounce+123@, noreply=user=host@). Strip it before matching, regardless
    # of whether the provider is on the plus-addressing list — these are
    # generated by senders, not by mailbox owners.
    base = re.split(r"[+=]", local, maxsplit=1)[0]

    if base in _ROLE_LOCAL_PARTS or local in _ROLE_LOCAL_PARTS:
        return True
    return any(s in local for s in _ROLE_SUBSTRINGS)


def is_bulk_sender(headers: Optional[Dict[str, str]] = None) -> bool:
    """Detect machine-generated mail from RFC headers.

    Far more reliable than guessing from content: a message carrying
    `List-Unsubscribe` or `Precedence: bulk` is telling you outright.
    """
    if not headers:
        return False
    lowered = {k.lower(): (v or "").lower() for k, v in headers.items()}

    if "list-unsubscribe" in lowered or "list-id" in lowered:
        return True
    if lowered.get("precedence", "") in ("bulk", "list", "junk"):
        return True
    if lowered.get("auto-submitted", "no") not in ("no", ""):
        return True
    if "x-auto-response-suppress" in lowered:
        return True
    return False


# ---------------------------------------------------------------------------
# Display names
# ---------------------------------------------------------------------------

_NAME_NOISE = re.compile(r"[^\w\s'-]", re.UNICODE)
_TITLES = {
    "dr", "prof", "professor", "mr", "mrs", "ms", "miss", "sir", "rev",
    "phd", "md", "jr", "sr", "ii", "iii", "esq",
}

#: Surnames common enough that a name match alone means nothing. A shared
#: "John Smith" is weak evidence; a shared unusual name is strong.
_COMMON_SURNAMES = {
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller",
    "davis", "rodriguez", "martinez", "hernandez", "lopez", "gonzalez",
    "wilson", "anderson", "thomas", "taylor", "moore", "jackson", "martin",
    "lee", "perez", "thompson", "white", "harris", "sanchez", "clark",
    "ramirez", "lewis", "robinson", "walker", "young", "allen", "king",
    "wright", "scott", "torres", "nguyen", "hill", "flores", "green",
    "adams", "nelson", "baker", "hall", "rivera", "campbell", "mitchell",
    "carter", "roberts", "chen", "wang", "li", "zhang", "liu", "kim",
    "patel", "singh", "kumar", "khan", "ali", "das", "sharma",
}


def name_key(display_name: str) -> str:
    """Normalised name for comparison: titles stripped, punctuation folded."""
    if not display_name:
        return ""
    cleaned = _NAME_NOISE.sub(" ", display_name.strip().lower())
    parts = [p for p in cleaned.split() if p and p.strip(".") not in _TITLES]
    return " ".join(parts)


def is_common_name(display_name: str) -> bool:
    """Would a match on this name be weak evidence?

    Also treats single-token names ("Mom", "Alex") as common: they collide
    constantly and carry no domain information.
    """
    key = name_key(display_name)
    if not key:
        return True
    tokens = key.split()
    if len(tokens) < 2:
        return True
    return tokens[-1] in _COMMON_SURNAMES


def names_agree(a: str, b: str) -> bool:
    """Same person's name, allowing for initials and ordering.

    Matches "John Smith" ↔ "J. Smith" ↔ "Smith, John", and deliberately does
    not match "John Smith" ↔ "Jane Smith".
    """
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True

    ta, tb = ka.split(), kb.split()
    if len(ta) < 2 or len(tb) < 2:
        return False

    # Compare as unordered sets so "Smith, John" matches "John Smith".
    sa, sb = sorted(ta), sorted(tb)
    if sa == sb:
        return True

    # Allow one side to abbreviate given names to initials.
    if len(ta) == len(tb):
        matched = 0
        for x, y in zip(sorted(ta), sorted(tb)):
            if x == y:
                matched += 1
            elif len(x) == 1 and y.startswith(x):
                matched += 1
            elif len(y) == 1 and x.startswith(y):
                matched += 1
            else:
                return False
        return matched == len(ta)

    return False


# ---------------------------------------------------------------------------
# Link proposals
# ---------------------------------------------------------------------------

class MatchSignal(str, Enum):
    """Ordered by reliability; see SIGNAL_CONFIDENCE."""

    USER_CONFIRMED = "user_confirmed"
    SYSTEM_CONTACTS = "system_contacts"  # macOS Contacts / Google Contacts
    SAME_IDENTITY = "same_identity"
    SIGNATURE_BLOCK = "signature_block"  # signature listed the other handle
    NAME_AND_DOMAIN = "name_and_domain"  # same name, same org domain
    RARE_NAME = "rare_name"  # same unusual name across sources
    COMMON_NAME = "common_name"  # same ordinary name — weak
    THREAD_COOCCURRENCE = "thread_cooccurrence"


SIGNAL_CONFIDENCE: Dict[MatchSignal, float] = {
    MatchSignal.USER_CONFIRMED: 1.00,
    MatchSignal.SYSTEM_CONTACTS: 0.98,
    MatchSignal.SAME_IDENTITY: 1.00,
    MatchSignal.SIGNATURE_BLOCK: 0.85,
    MatchSignal.NAME_AND_DOMAIN: 0.80,
    MatchSignal.RARE_NAME: 0.70,
    MatchSignal.COMMON_NAME: 0.30,
    MatchSignal.THREAD_COOCCURRENCE: 0.25,
}


class ResolutionBand(str, Enum):
    AUTO_LINK = "auto_link"
    SUGGEST = "suggest"  # ask the user
    IGNORE = "ignore"


AUTO_LINK_THRESHOLD = 0.90
SUGGEST_THRESHOLD = 0.60


def band_for(confidence: float) -> ResolutionBand:
    if confidence >= AUTO_LINK_THRESHOLD:
        return ResolutionBand.AUTO_LINK
    if confidence >= SUGGEST_THRESHOLD:
        return ResolutionBand.SUGGEST
    return ResolutionBand.IGNORE


@dataclass
class IdentityRecord:
    """Minimal view of an identity, for resolution."""

    id: str
    kind: IdentityKind
    value: str  # already normalised
    display_names: List[str] = field(default_factory=list)
    is_role: bool = False

    @property
    def domain(self) -> str:
        return self.value.rpartition("@")[2] if "@" in self.value else ""


@dataclass
class LinkProposal:
    identity_id: str
    other_identity_id: str
    confidence: float
    signal: MatchSignal
    evidence: str = ""

    @property
    def band(self) -> ResolutionBand:
        return band_for(self.confidence)


#: Domains where sharing a domain implies nothing about identity.
_PUBLIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me",
    "protonmail.com", "gmx.com", "mail.com", "zoho.com", "yandex.com",
    "fastmail.com", "msn.com", "comcast.net", "verizon.net",
}


def is_public_domain(domain: str) -> bool:
    return domain.lower() in _PUBLIC_DOMAINS


def propose_link(
    a: IdentityRecord, b: IdentityRecord, *, signature_handles: Optional[Set[str]] = None
) -> Optional[LinkProposal]:
    """Strongest signal linking two identities, or None.

    Deterministic checks first, probabilistic second — and role accounts are
    never linked to anyone, because they are not people.
    """
    if a.id == b.id:
        return None
    if a.is_role or b.is_role:
        return None

    if a.value and a.value == b.value:
        return LinkProposal(
            a.id, b.id, SIGNAL_CONFIDENCE[MatchSignal.SAME_IDENTITY],
            MatchSignal.SAME_IDENTITY, f"identical handle {a.value}",
        )

    # A signature that lists the other handle is near-proof: the person
    # published the association themselves.
    if signature_handles and b.value in signature_handles:
        return LinkProposal(
            a.id, b.id, SIGNAL_CONFIDENCE[MatchSignal.SIGNATURE_BLOCK],
            MatchSignal.SIGNATURE_BLOCK, f"signature lists {b.value}",
        )

    best: Optional[LinkProposal] = None
    for name_a in a.display_names:
        for name_b in b.display_names:
            if not names_agree(name_a, name_b):
                continue

            same_org = (
                a.domain
                and a.domain == b.domain
                and not is_public_domain(a.domain)
            )
            if same_org:
                signal = MatchSignal.NAME_AND_DOMAIN
                evidence = f"{name_a!r} at shared domain {a.domain}"
            elif is_common_name(name_a):
                signal = MatchSignal.COMMON_NAME
                evidence = f"{name_a!r} — common name, weak evidence"
            else:
                signal = MatchSignal.RARE_NAME
                evidence = f"{name_a!r} — distinctive name"

            confidence = SIGNAL_CONFIDENCE[signal]
            if best is None or confidence > best.confidence:
                best = LinkProposal(a.id, b.id, confidence, signal, evidence)

    return best


def propose_links(
    identities: Sequence[IdentityRecord],
    *,
    signature_handles: Optional[Dict[str, Set[str]]] = None,
) -> List[LinkProposal]:
    """All proposals above the ignore threshold, strongest first.

    Blocking keeps this from being O(n²) on a real corpus: identities are only
    compared within a block sharing a normalised name token or a private
    domain. Comparing every pair is unnecessary — two identities with nothing
    in common can never produce a signal above the floor.
    """
    signature_handles = signature_handles or {}
    blocks: Dict[str, List[IdentityRecord]] = {}

    for ident in identities:
        if ident.is_role:
            continue
        keys: Set[str] = set()
        for name in ident.display_names:
            for token in name_key(name).split():
                if len(token) > 1:
                    keys.add(f"n:{token}")
        if ident.domain and not is_public_domain(ident.domain):
            keys.add(f"d:{ident.domain}")
        if ident.value:
            keys.add(f"v:{ident.value}")
        for key in keys:
            blocks.setdefault(key, []).append(ident)

    seen: Set[tuple] = set()
    proposals: List[LinkProposal] = []
    for members in blocks.values():
        if len(members) < 2:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                pair = tuple(sorted((a.id, b.id)))
                if pair in seen:
                    continue
                seen.add(pair)
                proposal = propose_link(
                    a, b, signature_handles=signature_handles.get(a.id)
                )
                if proposal and proposal.band is not ResolutionBand.IGNORE:
                    proposals.append(proposal)

    proposals.sort(key=lambda p: p.confidence, reverse=True)
    return proposals


# ---------------------------------------------------------------------------
# Signature parsing
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")


def extract_handles_from_signature(signature: str) -> Set[str]:
    """Handles a person published about themselves in their sign-off.

    Strong linking evidence — the person asserted the association. Runs on the
    signature block only, never the body: an email body full of other people's
    addresses would produce nonsense links.
    """
    found: Set[str] = set()
    if not signature:
        return found
    for match in _EMAIL_RE.findall(signature):
        found.add(normalize_email(match))
    for match in _PHONE_RE.findall(signature):
        normalised = normalize_phone(match)
        if normalised:
            found.add(normalised)
    return found


def merge_display_names(existing: Iterable[str], incoming: str) -> List[str]:
    """Accumulate the name variants seen for an identity, most recent first.

    Keeping every variant matters: the display name in one message may be the
    only bridge to another identity, and overwriting loses it.
    """
    out = [incoming.strip()] if incoming and incoming.strip() else []
    for name in existing:
        if name and name.strip() and name.strip().lower() not in {
            n.lower() for n in out
        }:
            out.append(name.strip())
    return out[:10]
