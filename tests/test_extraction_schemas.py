"""Extraction contract: grounding, date sanity, and post-processing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.pipeline.schemas import (
    Category,
    CommitmentKind,
    CommitmentUpdate,
    DuePrecision,
    ExtractedCommitment,
    MessageAnalysis,
    Owner,
    check_due_date,
    sanitise,
    verify_grounding,
)

EMAIL = """Hi,

Just a reminder that the research proposal is due by Friday the 15th at 5pm.
Please send it to me directly rather than the department address.

Also, could you confirm whether you're attending the Thursday seminar?

Best,
Professor Smith
"""

SENT_AT = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)


def commitment(**kw) -> ExtractedCommitment:
    base = dict(
        kind=CommitmentKind.DEADLINE,
        title="Submit research proposal",
        owner=Owner.ME,
        due_at=datetime(2026, 8, 15, 17, 0, tzinfo=timezone.utc),
        due_precision=DuePrecision.EXACT,
        confidence=0.9,
        evidence_quote="the research proposal is due by Friday the 15th at 5pm",
    )
    base.update(kw)
    return ExtractedCommitment(**base)


# ------------------------------------------------------------------ grounding

def test_exact_quote_is_grounded():
    r = verify_grounding("the research proposal is due by Friday the 15th at 5pm", EMAIL)
    assert r.grounded and r.method == "exact"


def test_quote_across_a_line_break_is_grounded():
    """Models collapse newlines to spaces; that is not a hallucination."""
    r = verify_grounding(
        "Just a reminder that the research proposal is due by Friday the 15th at 5pm.",
        EMAIL,
    )
    assert r.grounded


def test_smart_quotes_and_dashes_do_not_break_grounding():
    source = "Please don't forget - the report is due Friday."
    r = verify_grounding("Please don’t forget — the report is due Friday.", source)
    assert r.grounded


def test_fabricated_quote_is_rejected():
    r = verify_grounding("your tuition payment of $4,200 is overdue", EMAIL)
    assert not r.grounded
    assert "appears in the message" in r.reason


def test_slightly_reworded_quote_passes_on_overlap():
    r = verify_grounding("research proposal is due by Friday the 15th", EMAIL)
    assert r.grounded


def test_empty_quote_is_not_grounded():
    assert not verify_grounding("", EMAIL).grounded


def test_very_short_quote_cannot_be_verified():
    r = verify_grounding("due", EMAIL)
    assert not r.grounded
    assert "too short" in r.reason


# ----------------------------------------------------------------- date sanity

def test_future_deadline_is_accepted():
    assert check_due_date(SENT_AT + timedelta(days=3), SENT_AT).ok


def test_backdated_deadline_is_rejected():
    """'Friday' resolved to last Friday instead of next — a real failure mode."""
    check = check_due_date(SENT_AT - timedelta(days=5), SENT_AT)
    assert not check.ok
    assert check.downgrade_to == DuePrecision.VAGUE


def test_same_day_deadline_is_fine():
    assert check_due_date(SENT_AT + timedelta(hours=6), SENT_AT).ok


def test_slightly_backdated_is_tolerated():
    """Timezone skew of a few hours must not discard a real deadline."""
    assert check_due_date(SENT_AT - timedelta(hours=6), SENT_AT).ok


def test_absurdly_distant_deadline_is_rejected():
    assert not check_due_date(SENT_AT + timedelta(days=365 * 9), SENT_AT).ok


def test_no_deadline_is_valid():
    assert check_due_date(None, SENT_AT).ok


def test_naive_datetimes_are_coerced_to_utc():
    c = commitment(due_at=datetime(2026, 8, 15, 17, 0))
    assert c.due_at.tzinfo is not None


# ------------------------------------------------------------------- sanitise

def test_grounded_commitment_survives():
    result = sanitise(
        MessageAnalysis(commitments=[commitment()]),
        source_text=EMAIL,
        message_time=SENT_AT,
    )
    assert len(result.analysis.commitments) == 1
    assert result.dropped == []


def test_hallucinated_commitment_is_dropped():
    fake = commitment(
        title="Pay tuition balance",
        evidence_quote="your tuition balance of $4,200 is due immediately",
    )
    result = sanitise(
        MessageAnalysis(commitments=[commitment(), fake]),
        source_text=EMAIL,
        message_time=SENT_AT,
    )
    titles = [c.title for c in result.analysis.commitments]
    assert titles == ["Submit research proposal"]
    assert any("ungrounded" in d for d in result.dropped)


def test_low_confidence_is_dropped():
    result = sanitise(
        MessageAnalysis(commitments=[commitment(confidence=0.1)]),
        source_text=EMAIL,
        message_time=SENT_AT,
    )
    assert result.analysis.commitments == []
    assert any("confidence" in d for d in result.dropped)


def test_bad_date_loses_the_date_but_keeps_the_task():
    """A wrong deadline is worse than no deadline — but the task is still real."""
    result = sanitise(
        MessageAnalysis(commitments=[commitment(due_at=SENT_AT - timedelta(days=30))]),
        source_text=EMAIL,
        message_time=SENT_AT,
    )
    assert len(result.analysis.commitments) == 1
    kept = result.analysis.commitments[0]
    assert kept.due_at is None
    assert kept.due_precision == DuePrecision.VAGUE
    assert any("dropped date" in d for d in result.dropped)


def test_automated_messages_cannot_demand_action():
    """Marketing 'ACT NOW' must not reach the notification pipeline."""
    result = sanitise(
        MessageAnalysis(
            category=Category.PROMOTIONAL,
            is_automated=True,
            requires_action=True,
            importance=0.9,
        ),
        source_text=EMAIL,
        message_time=SENT_AT,
    )
    assert result.analysis.requires_action is False


def test_human_message_keeps_requires_action():
    result = sanitise(
        MessageAnalysis(category=Category.SCHOOL, is_automated=False, requires_action=True),
        source_text=EMAIL,
        message_time=SENT_AT,
    )
    assert result.analysis.requires_action is True


def test_ungrounded_update_is_dropped():
    result = sanitise(
        MessageAnalysis(
            updates=[
                CommitmentUpdate(commitment_id="t1", evidence_quote="cancel everything")
            ]
        ),
        source_text=EMAIL,
        message_time=SENT_AT,
    )
    assert result.analysis.updates == []


def test_update_without_a_quote_is_allowed():
    """Status changes inferred from context need not quote anything."""
    result = sanitise(
        MessageAnalysis(updates=[CommitmentUpdate(commitment_id="t1")]),
        source_text=EMAIL,
        message_time=SENT_AT,
    )
    assert len(result.analysis.updates) == 1


def test_owner_distinguishes_my_task_from_waiting_on_them():
    mine = commitment(owner=Owner.ME)
    theirs = commitment(
        owner=Owner.THEM,
        kind=CommitmentKind.PROMISE,
        title="Professor sends feedback",
        evidence_quote="Please send it to me directly",
    )
    result = sanitise(
        MessageAnalysis(commitments=[mine, theirs]),
        source_text=EMAIL,
        message_time=SENT_AT,
    )
    owners = {c.owner for c in result.analysis.commitments}
    assert owners == {Owner.ME, Owner.THEM}


def test_people_are_deduplicated_case_insensitively():
    a = MessageAnalysis(people=["Professor Smith", "professor smith", "Alex"])
    assert a.people == ["Professor Smith", "Alex"]


def test_schema_is_json_serialisable_for_ollama_format():
    from backend.pipeline.schemas import analysis_json_schema

    schema = analysis_json_schema()
    assert schema["type"] == "object"
    assert "commitments" in schema["properties"]


def test_confidence_must_be_a_probability():
    with pytest.raises(ValueError):
        commitment(confidence=1.5)
