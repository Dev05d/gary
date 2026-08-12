"""Data horizon — what Gary is allowed to claim it knows.

Live-only ingestion creates a correctness problem that is easy to miss and
severe when it bites.

Gary connects to Gmail on 12 August. On 20 August you ask:

    "What did Professor Smith say about my project last month?"

There is nothing in the database. A naive agent answers "I couldn't find
anything from Professor Smith about your project" — which is
**indistinguishable from him never having written**. The user has no way to
tell "it didn't happen" from "I wasn't watching yet", and will trust the wrong
one.

The fix is to make the horizon a first-class fact the agent must consult:
every source records when it started recording, and any query whose time range
reaches behind that point is answered with an explicit gap warning rather than
a bare negative.

This module is pure logic over horizon data — no I/O — so the rules are
testable without a database or a Gmail account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Optional, Sequence


class Coverage(str, Enum):
    FULL = "full"  # the whole requested range is covered
    PARTIAL = "partial"  # the range starts before we began recording
    NONE = "none"  # the range ends before we began recording
    FUTURE = "future"  # the range is entirely in the future
    UNKNOWN = "unknown"  # no source connected at all


@dataclass(frozen=True)
class SourceHorizon:
    """When a source started producing data, and how fresh it is."""

    kind: str  # "gmail" | "gcal" | "imessage"
    display_name: str
    #: The moment ingestion began. Nothing before this exists locally.
    recording_since: Optional[datetime]
    #: Most recent successful sync — staleness, not coverage.
    last_sync_at: Optional[datetime] = None
    connected: bool = True
    #: Calendar syncs a window rather than a watermark, so it genuinely does
    #: hold future data. Message sources never do.
    covers_future: bool = False
    #: Forward edge for windowed sources.
    covered_until: Optional[datetime] = None


@dataclass
class TimeRange:
    start: Optional[datetime] = None
    end: Optional[datetime] = None

    def clamp_to(self, floor: datetime) -> "TimeRange":
        return TimeRange(
            start=floor if self.start is None or self.start < floor else self.start,
            end=self.end,
        )


@dataclass
class CoverageReport:
    coverage: Coverage
    #: Human-readable caveat the agent must include in its answer, or None.
    warning: Optional[str]
    #: The portion of the request that can actually be answered.
    effective_range: TimeRange
    #: Sources consulted, for citation.
    sources: List[str]

    @property
    def must_warn(self) -> bool:
        return self.coverage in (Coverage.PARTIAL, Coverage.NONE, Coverage.UNKNOWN)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(dt: datetime) -> str:
    return _utc(dt).strftime("%-d %B %Y") if hasattr(dt, "strftime") else str(dt)


def assess_coverage(
    requested: TimeRange,
    horizons: Sequence[SourceHorizon],
    *,
    now: Optional[datetime] = None,
) -> CoverageReport:
    """Can the requested range be answered honestly?

    Uses the *earliest* horizon across the relevant sources: if Gmail has been
    recording since June and iMessage since August, a June question is
    partially answerable and the answer must say which part.
    """
    now = _utc(now or datetime.now(timezone.utc))

    active = [h for h in horizons if h.connected and h.recording_since is not None]
    if not active:
        names = ", ".join(h.display_name for h in horizons) or "any source"
        return CoverageReport(
            coverage=Coverage.UNKNOWN,
            warning=(
                f"No data source is connected yet ({names}), so I have nothing to "
                "search. Connect an account in Settings first."
            ),
            effective_range=requested,
            sources=[],
        )

    earliest = min(_utc(h.recording_since) for h in active)  # type: ignore[arg-type]
    source_names = [h.display_name for h in active]

    # A purely future request is only answerable by a windowed source.
    if requested.start and _utc(requested.start) > now:
        forward = [h for h in active if h.covers_future]
        if not forward:
            return CoverageReport(
                coverage=Coverage.FUTURE,
                warning=(
                    "That period is in the future. I can only see scheduled "
                    "calendar events ahead of today, not messages."
                ),
                effective_range=requested,
                sources=[],
            )
        return CoverageReport(
            coverage=Coverage.FULL,
            warning=None,
            effective_range=requested,
            sources=[h.display_name for h in forward],
        )

    if requested.start is None or _utc(requested.start) >= earliest:
        return CoverageReport(
            coverage=Coverage.FULL,
            warning=None,
            effective_range=requested,
            sources=source_names,
        )

    # The request reaches behind the horizon.
    if requested.end is not None and _utc(requested.end) <= earliest:
        return CoverageReport(
            coverage=Coverage.NONE,
            warning=(
                f"I have no data from that period. I only started recording on "
                f"{_fmt(earliest)}, so anything before that was never captured — "
                "this is a gap in my records, not evidence that nothing happened."
            ),
            effective_range=TimeRange(start=earliest, end=requested.end),
            sources=source_names,
        )

    return CoverageReport(
        coverage=Coverage.PARTIAL,
        warning=(
            f"I only have data from {_fmt(earliest)} onward, so this covers part "
            "of the period you asked about. Anything earlier was never captured."
        ),
        effective_range=requested.clamp_to(earliest),
        sources=source_names,
    )


#: A sync gap longer than this means the answer may be missing recent data.
STALENESS_THRESHOLD = timedelta(minutes=30)


def staleness_warning(
    horizons: Sequence[SourceHorizon], *, now: Optional[datetime] = None
) -> Optional[str]:
    """Warn when a connected source has not synced recently.

    Distinct from coverage: this is "my recent data may be incomplete", not
    "that period does not exist". A laptop that slept overnight produces this.
    """
    now = _utc(now or datetime.now(timezone.utc))
    stale: List[str] = []

    for h in horizons:
        if not h.connected:
            continue
        if h.last_sync_at is None:
            stale.append(f"{h.display_name} (never synced)")
            continue
        gap = now - _utc(h.last_sync_at)
        if gap > STALENESS_THRESHOLD:
            stale.append(f"{h.display_name} (last synced {_describe_gap(gap)} ago)")

    if not stale:
        return None
    return (
        "Heads up — these sources are behind, so very recent items may be "
        f"missing: {', '.join(stale)}."
    )


def _describe_gap(gap: timedelta) -> str:
    minutes = int(gap.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


def horizon_prompt_block(
    horizons: Sequence[SourceHorizon], *, now: Optional[datetime] = None
) -> str:
    """Statement of record boundaries, injected into the agent's system prompt.

    The model cannot reason about what it does not know unless the boundary is
    stated. This is trusted content — it describes Gary's own state, not
    anything retrieved from a message.
    """
    now = _utc(now or datetime.now(timezone.utc))
    if not horizons:
        return "DATA HORIZON: no sources connected. You have no personal data to search."

    lines = ["DATA HORIZON — the limits of what you can know:"]
    for h in horizons:
        if not h.connected:
            lines.append(f"  - {h.display_name}: NOT CONNECTED. No data at all.")
        elif h.recording_since is None:
            lines.append(f"  - {h.display_name}: connected, nothing ingested yet.")
        else:
            span = f"records begin {_fmt(h.recording_since)}"
            if h.covers_future and h.covered_until:
                span += f"; covered through {_fmt(h.covered_until)}"
            lines.append(f"  - {h.display_name}: {span}")

    lines.append(
        "\nIf a question concerns a period before a source's records begin, say so "
        "explicitly. Finding nothing from before that point means it was never "
        "recorded — it is NOT evidence the thing did not happen. Never present a "
        "gap in coverage as a negative finding."
    )
    return "\n".join(lines)
