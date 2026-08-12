"""Adversarial and malformed input.

Everything here is real input the pipeline will eventually see: broken headers
from ancient mail clients, unicode tricks, data that contradicts itself, and
the occasional attempt to make the system misbehave on purpose.

The bar is not "produces a good answer" — it is "does not crash, does not
silently merge two people, and does not fabricate".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.pipeline.horizon import (
    Coverage,
    SourceHorizon,
    TimeRange,
    assess_coverage,
    staleness_warning,
)
from backend.pipeline.identity import (
    IdentityKind,
    IdentityRecord,
    ResolutionBand,
    is_role_account,
    name_key,
    names_agree,
    normalize_email,
    normalize_handle,
    normalize_phone,
    propose_link,
    propose_links,
)
from backend.pipeline.importance import SenderBehaviour, score_importance


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def ident(id_, value, *names, kind=IdentityKind.EMAIL, role=False) -> IdentityRecord:
    return IdentityRecord(
        id=id_, kind=kind, value=value, display_names=list(names), is_role=role
    )


# ===========================================================================
# Malformed email addresses
# ===========================================================================

@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", "@", "@example.com", "a@", "not-an-email",
        "a@b@c.com", "<>", "<<a@b.com>>", "a b@c.com",
        "\"quoted local\"@example.com", "a@[192.168.1.1]",
        "a@b..com", ".@example.com", "a@.com",
    ],
)
def test_malformed_addresses_never_raise(raw):
    """A single broken From: header must not abort a sync batch."""
    result = normalize_email(raw)
    assert isinstance(result, str)


def test_absurdly_long_address_is_handled():
    long_local = "x" * 5000
    result = normalize_email(f"{long_local}@example.com")
    assert result.endswith("@example.com")


def test_empty_local_part_does_not_become_a_role_account():
    """'@example.com' has no local part; it must not match a role name."""
    assert not is_role_account("@example.com")


def test_role_detection_on_garbage_is_safe():
    for raw in ("", "   ", "@", "no-at-sign", None):
        assert is_role_account(raw or "") in (True, False)


# ===========================================================================
# Unicode and homoglyph attacks
# ===========================================================================

def test_cyrillic_lookalike_domain_is_not_folded_into_the_latin_one():
    """'аpple.com' with a Cyrillic 'а' is a different domain, and must stay one.

    Folding these together would let a phishing domain inherit the trust of
    the real one.
    """
    latin = normalize_email("support@apple.com")
    cyrillic = normalize_email("support@аpple.com")
    assert latin != cyrillic


def test_unicode_display_names_do_not_crash_name_matching():
    for name in ("张伟", "Ægir Þórsson", "Владимир", "علي", "🙂 Bob", "​Bob"):
        assert isinstance(name_key(name), str)
        assert names_agree(name, name) in (True, False)


def test_names_that_reduce_to_nothing_do_not_match():
    """'Dr.' and 'Prof.' both strip to empty — that is not a name match."""
    assert not names_agree("Dr.", "Prof.")
    assert not names_agree("...", "???")


def test_zero_width_characters_do_not_create_phantom_names():
    assert name_key("​‌‍") in ("", "​‌‍".strip())


def test_rtl_text_is_handled():
    assert isinstance(name_key("محمد علي"), str)


# ===========================================================================
# Phone numbers
# ===========================================================================

@pytest.mark.parametrize(
    "raw",
    ["1-800-FLOWERS", "555-1234 x89", "+", "++1555", "()", "---", "+00000000000000000000"],
)
def test_weird_phone_input_never_raises(raw):
    result = normalize_phone(raw)
    assert result is None or result.startswith("+")


def test_extension_is_not_silently_folded_into_the_number():
    """'555-010-2030 x89' must not become a different valid-looking number."""
    plain = normalize_phone("555-010-2030")
    with_ext = normalize_phone("555-010-2030 x89")
    assert with_ext != plain or with_ext is None


def test_handle_normalisation_never_raises():
    for raw in ("", "   ", "@", "+", "not a handle", "a@b@c"):
        assert isinstance(normalize_handle(raw), str)


# ===========================================================================
# Identity linking under hostile input
# ===========================================================================

def test_identity_with_no_display_names_produces_no_link():
    a = ident("i1", "a@corp.com")
    b = ident("i2", "b@corp.com")
    assert propose_link(a, b) is None


def test_empty_string_values_do_not_link_as_identical():
    """Two identities with blank handles are not 'the same handle'."""
    a = ident("i1", "", "Alice")
    b = ident("i2", "", "Bob")
    result = propose_link(a, b)
    assert result is None or result.signal.value != "same_identity"


def test_hostile_display_name_cannot_force_a_merge():
    """A sender controls their display name; it must not be enough on its own."""
    victim = ident("i1", "ceo@corp.com", "Jane Doe")
    attacker = ident("i2", "attacker@evil.com", "Jane Doe")
    proposal = propose_link(victim, attacker)
    assert proposal is None or proposal.band is not ResolutionBand.AUTO_LINK


def test_thousands_of_identities_do_not_explode():
    """Blocking must keep batch resolution off the O(n^2) path."""
    identities = [
        ident(f"i{i}", f"user{i}@example{i % 50}.com", f"Person {i}")
        for i in range(2000)
    ]
    proposals = propose_links(identities)
    assert isinstance(proposals, list)


def test_identity_list_with_duplicates_of_itself():
    a = ident("i1", "a@corp.com", "Alice Smith")
    assert propose_links([a, a, a]) == []


# ===========================================================================
# Horizon: inverted, absurd, and boundary ranges
# ===========================================================================

def gmail(since=datetime(2026, 8, 12, tzinfo=timezone.utc)) -> SourceHorizon:
    return SourceHorizon(
        kind="gmail", display_name="Gmail", recording_since=since, last_sync_at=NOW
    )


def test_inverted_range_does_not_crash():
    """end before start — a malformed query must degrade, not explode."""
    report = assess_coverage(
        TimeRange(start=NOW, end=NOW - timedelta(days=30)), [gmail()], now=NOW
    )
    assert report.coverage in set(Coverage)


def test_range_exactly_on_the_horizon_boundary():
    horizon = datetime(2026, 8, 12, tzinfo=timezone.utc)
    report = assess_coverage(
        TimeRange(start=horizon, end=NOW), [gmail(since=horizon)], now=NOW
    )
    assert report.coverage is Coverage.FULL, "boundary is inclusive"


def test_one_microsecond_before_the_horizon_is_partial():
    horizon = datetime(2026, 8, 12, tzinfo=timezone.utc)
    report = assess_coverage(
        TimeRange(start=horizon - timedelta(microseconds=1), end=NOW),
        [gmail(since=horizon)],
        now=NOW,
    )
    assert report.coverage is Coverage.PARTIAL


def test_naive_datetimes_are_accepted():
    """A caller that forgets tzinfo must not silently get wrong answers."""
    naive = SourceHorizon(
        kind="gmail",
        display_name="Gmail",
        recording_since=datetime(2026, 8, 12),  # naive
        last_sync_at=datetime(2026, 8, 20, 12, 0),
    )
    report = assess_coverage(
        TimeRange(start=datetime(2026, 8, 15)), [naive], now=NOW
    )
    assert report.coverage is Coverage.FULL


def test_epoch_and_far_future_ranges():
    for start in (datetime(1970, 1, 1, tzinfo=timezone.utc), datetime(2200, 1, 1, tzinfo=timezone.utc)):
        report = assess_coverage(TimeRange(start=start), [gmail()], now=NOW)
        assert report.coverage in set(Coverage)


def test_clock_skew_last_sync_in_the_future():
    """A corrected system clock can make last_sync_at appear ahead of now."""
    future = SourceHorizon(
        kind="gmail",
        display_name="Gmail",
        recording_since=datetime(2026, 8, 12, tzinfo=timezone.utc),
        last_sync_at=NOW + timedelta(hours=3),
    )
    assert staleness_warning([future], now=NOW) is None


# ===========================================================================
# Importance with contradictory data
# ===========================================================================

def test_more_replies_than_messages_is_clamped():
    """Data drift must not produce a reply rate above 1."""
    weird = SenderBehaviour(messages_received=5, messages_replied_to=50)
    result = score_importance(0.5, weird)
    assert 0.0 <= result.score <= 1.0


def test_negative_counts_do_not_produce_negative_scores():
    weird = SenderBehaviour(messages_received=-10, messages_replied_to=-5)
    result = score_importance(0.5, weird)
    assert 0.0 <= result.score <= 1.0


def test_negative_reply_latency_is_survivable():
    """A reply timestamped before the message it answers — clock skew."""
    weird = SenderBehaviour(
        messages_received=10,
        messages_replied_to=10,
        median_reply_latency=timedelta(seconds=-3600),
    )
    assert 0.0 <= score_importance(0.5, weird).score <= 1.0


def test_everything_zero_is_cold_start():
    assert score_importance(0.5, SenderBehaviour()).is_cold_start


def test_nan_like_extremes():
    for model in (float("inf"), float("-inf")):
        score = score_importance(model, None).score
        assert 0.0 <= score <= 1.0
