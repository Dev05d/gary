"""Data horizon: never present a coverage gap as a negative finding."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.pipeline.horizon import (
    Coverage,
    SourceHorizon,
    TimeRange,
    assess_coverage,
    horizon_prompt_block,
    staleness_warning,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
CONNECTED_AUG_12 = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)


def gmail(since=CONNECTED_AUG_12, last_sync=NOW, connected=True) -> SourceHorizon:
    return SourceHorizon(
        kind="gmail",
        display_name="Gmail",
        recording_since=since,
        last_sync_at=last_sync,
        connected=connected,
    )


def calendar() -> SourceHorizon:
    return SourceHorizon(
        kind="gcal",
        display_name="Google Calendar",
        recording_since=CONNECTED_AUG_12 - timedelta(days=7),
        last_sync_at=NOW,
        covers_future=True,
        covered_until=NOW + timedelta(days=90),
    )


def days(n: int) -> datetime:
    return NOW - timedelta(days=n)


# ------------------------------------------------------------------ coverage

def test_range_inside_the_horizon_is_fully_covered():
    report = assess_coverage(TimeRange(start=days(3), end=NOW), [gmail()], now=NOW)
    assert report.coverage is Coverage.FULL
    assert report.warning is None
    assert not report.must_warn


def test_range_entirely_before_the_horizon_warns_loudly():
    """The failure this whole module exists to prevent."""
    report = assess_coverage(
        TimeRange(start=days(60), end=days(40)), [gmail()], now=NOW
    )
    assert report.coverage is Coverage.NONE
    assert report.must_warn
    assert "12 August 2026" in report.warning
    assert "not evidence that nothing happened" in report.warning


def test_range_straddling_the_horizon_is_partial_and_clamped():
    report = assess_coverage(TimeRange(start=days(30), end=NOW), [gmail()], now=NOW)
    assert report.coverage is Coverage.PARTIAL
    assert report.must_warn
    assert report.effective_range.start == CONNECTED_AUG_12
    assert "part of the period" in report.warning


def test_open_ended_recent_query_is_covered():
    report = assess_coverage(TimeRange(start=None, end=None), [gmail()], now=NOW)
    assert report.coverage is Coverage.FULL


def test_no_connected_source_says_so():
    report = assess_coverage(
        TimeRange(start=days(1)), [gmail(connected=False)], now=NOW
    )
    assert report.coverage is Coverage.UNKNOWN
    assert report.must_warn
    assert "Connect an account" in report.warning


def test_connected_but_nothing_ingested_yet():
    report = assess_coverage(TimeRange(start=days(1)), [gmail(since=None)], now=NOW)
    assert report.coverage is Coverage.UNKNOWN


def test_earliest_horizon_across_sources_wins():
    """Gmail since June, iMessage since August → a July question is partial."""
    older = SourceHorizon(
        kind="gmail",
        display_name="Gmail",
        recording_since=datetime(2026, 6, 1, tzinfo=timezone.utc),
        last_sync_at=NOW,
    )
    newer = SourceHorizon(
        kind="imessage",
        display_name="iMessage",
        recording_since=datetime(2026, 8, 12, tzinfo=timezone.utc),
        last_sync_at=NOW,
    )
    report = assess_coverage(
        TimeRange(start=datetime(2026, 7, 1, tzinfo=timezone.utc), end=NOW),
        [older, newer],
        now=NOW,
    )
    assert report.coverage is Coverage.FULL
    assert set(report.sources) == {"Gmail", "iMessage"}


# -------------------------------------------------------------------- future

def test_future_range_without_a_calendar_is_refused():
    report = assess_coverage(
        TimeRange(start=NOW + timedelta(days=1), end=NOW + timedelta(days=2)),
        [gmail()],
        now=NOW,
    )
    assert report.coverage is Coverage.FUTURE
    assert "in the future" in report.warning


def test_future_range_with_a_calendar_is_answerable():
    """Calendar syncs a window, so tomorrow genuinely exists locally."""
    report = assess_coverage(
        TimeRange(start=NOW + timedelta(days=1), end=NOW + timedelta(days=2)),
        [gmail(), calendar()],
        now=NOW,
    )
    assert report.coverage is Coverage.FULL
    assert report.sources == ["Google Calendar"]


# ----------------------------------------------------------------- staleness

def test_fresh_sources_produce_no_staleness_warning():
    assert staleness_warning([gmail(last_sync=NOW - timedelta(minutes=2))], now=NOW) is None


def test_stale_source_is_reported_with_a_readable_gap():
    warning = staleness_warning(
        [gmail(last_sync=NOW - timedelta(hours=14))], now=NOW
    )
    assert "Gmail" in warning
    assert "14 hours ago" in warning


def test_never_synced_is_reported():
    assert "never synced" in staleness_warning([gmail(last_sync=None)], now=NOW)


def test_disconnected_sources_are_not_reported_as_stale():
    """Disconnected is a coverage problem, not a freshness one."""
    assert staleness_warning([gmail(connected=False, last_sync=None)], now=NOW) is None


def test_multi_day_gap_reads_in_days():
    warning = staleness_warning([gmail(last_sync=NOW - timedelta(days=3))], now=NOW)
    assert "3 days ago" in warning


# -------------------------------------------------------------- prompt block

def test_prompt_block_states_the_boundary_and_the_rule():
    block = horizon_prompt_block([gmail(), calendar()], now=NOW)
    assert "Gmail: records begin 12 August 2026" in block
    assert "covered through" in block
    assert "NOT evidence the thing did not happen" in block


def test_prompt_block_names_disconnected_sources():
    block = horizon_prompt_block([gmail(connected=False)], now=NOW)
    assert "NOT CONNECTED" in block


def test_prompt_block_with_no_sources():
    assert "no sources connected" in horizon_prompt_block([], now=NOW)
