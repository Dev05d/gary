"""Triage: skip the LLM on obvious bulk without losing real commitments."""

from __future__ import annotations

import pytest

from backend.pipeline.triage import (
    AuditFinding,
    has_strong_temporal_language,
    TriageDecision,
    TriageInput,
    audit_needs_attention,
    evaluate_audit,
    has_action_language,
    has_temporal_language,
    should_audit,
    triage,
)

BULK_HEADERS = {"List-Unsubscribe": "<mailto:u@x.com>", "Precedence": "bulk"}


def msg(**kw) -> TriageInput:
    base = dict(
        subject="Hello",
        body_clean="Just checking in about the project, let me know how it's going.",
        sender_email="sarah@example.com",
        headers={},
    )
    base.update(kw)
    return TriageInput(**base)


# ------------------------------------------------------------------ language

@pytest.mark.parametrize(
    "text",
    [
        "the report is due Friday",
        "deadline is next Monday",
        "please submit by the 15th",
        "your subscription expires tomorrow",
        "meeting at 3pm",
        "invoice due 12/05/2026",
        "reply before Thursday",
        "due Aug 15",
    ],
)
def test_temporal_language_detected(text):
    assert has_temporal_language(text)


@pytest.mark.parametrize(
    "text", ["thanks for the update", "here is the file you wanted", "sounds good"]
)
def test_no_false_temporal_positives(text):
    assert not has_temporal_language(text)


@pytest.mark.parametrize(
    "text",
    [
        "please confirm your attendance",
        "can you review this",
        "action required",
        "let me know what you think",
        "payment outstanding",
    ],
)
def test_action_language_detected(text):
    assert has_action_language(text)


# ------------------------------------------------------------------ decisions

def test_human_mail_is_always_extracted():
    assert triage(msg()).decision is TriageDecision.EXTRACT


def test_my_own_sent_mail_is_extracted():
    """'I'll send the report Friday' is a commitment I made."""
    result = triage(
        msg(
            is_from_me=True,
            body_clean="I'll send you the report by Friday, promise.",
        )
    )
    assert result.decision is TriageDecision.EXTRACT
    assert "sent by me" in result.reasons


def test_plain_newsletter_skips_the_llm():
    result = triage(
        msg(
            subject="This week in tech",
            body_clean="Here are the top stories we picked for you this week.",
            sender_email="newsletter@media.com",
            headers=BULK_HEADERS,
        )
    )
    assert result.decision is TriageDecision.CLASSIFY_ONLY
    assert result.is_bulk


def test_bill_from_a_noreply_address_is_extracted():
    """The load-bearing exception — bills are real deadlines."""
    result = triage(
        msg(
            subject="Your electricity bill",
            body_clean="Your bill of $84.20 is due on the 15th. Pay online to avoid a late fee.",
            sender_email="noreply@utility.com",
            headers=BULK_HEADERS,
        )
    )
    assert result.decision is TriageDecision.EXTRACT
    assert any("deadline language" in r for r in result.reasons)


def test_appointment_reminder_from_a_role_account_is_extracted():
    result = triage(
        msg(
            subject="Appointment reminder",
            body_clean="You have a dental appointment on Tuesday at 2pm.",
            sender_email="reminders@clinic.com",
            headers={},
        )
    )
    assert result.decision is TriageDecision.EXTRACT


def test_marketing_urgency_does_not_buy_an_llm_call():
    """'Act now! Limited time!' is engineered to look urgent."""
    result = triage(
        msg(
            subject="LAST CHANCE",
            body_clean=(
                "Act now! Limited time offer, 50% off. Don't miss out. "
                "Shop now and register for exclusive offers. Free shipping."
            ),
            sender_email="deals@shop.com",
            headers=BULK_HEADERS,
        )
    )
    assert result.decision is TriageDecision.CLASSIFY_ONLY
    assert any("marketing" in r for r in result.reasons)


def test_marketing_with_a_real_date_still_gets_extracted():
    """A dated promo could be a genuine expiry the user cares about."""
    result = triage(
        msg(
            subject="Your trial ends",
            body_clean="Act now! Your free trial expires on 30 September. Shop now.",
            sender_email="noreply@saas.com",
            headers=BULK_HEADERS,
        )
    )
    assert result.decision is TriageDecision.EXTRACT


def test_very_short_reply_is_classify_only():
    result = triage(msg(subject="", body_clean="ok thanks"))
    assert result.decision is TriageDecision.CLASSIFY_ONLY


def test_short_reply_in_an_open_thread_is_extracted():
    """'Done!' closes a commitment and carries no date or action language."""
    result = triage(
        msg(subject="", body_clean="done!", thread_has_open_commitments=True)
    )
    assert result.decision is TriageDecision.EXTRACT
    assert "open commitments" in result.reasons[0]


def test_bulk_reply_in_an_open_thread_still_extracted():
    result = triage(
        msg(
            body_clean="Confirmed.",
            headers=BULK_HEADERS,
            thread_has_open_commitments=True,
        )
    )
    assert result.decision is TriageDecision.EXTRACT


def test_role_sender_is_flagged_even_when_extracted():
    result = triage(
        msg(
            sender_email="billing@service.com",
            body_clean="Payment of $20 is due Friday.",
        )
    )
    assert result.is_role_sender
    assert result.decision is TriageDecision.EXTRACT


# --------------------------------------------------------------------- audit

def test_audit_sampling_is_deterministic():
    first = [should_audit(f"m{i}", 0.2) for i in range(200)]
    second = [should_audit(f"m{i}", 0.2) for i in range(200)]
    assert first == second


def test_audit_sampling_hits_roughly_the_requested_rate():
    hits = sum(should_audit(f"msg-{i}", 0.1) for i in range(4000))
    assert 300 <= hits <= 500, f"expected ~10% of 4000, got {hits}"


def test_audit_rate_bounds():
    assert not should_audit("m1", 0.0)
    assert should_audit("m1", 1.0)


def test_audit_report_with_no_misses():
    report = evaluate_audit([], sampled=50)
    assert report.miss_rate == 0.0
    assert "missed nothing" in report.summary()


def test_audit_report_counts_only_real_misses():
    findings = [
        AuditFinding("m1", TriageDecision.CLASSIFY_ONLY, 2, missed=True),
        AuditFinding("m2", TriageDecision.CLASSIFY_ONLY, 0, missed=False),
    ]
    report = evaluate_audit(findings, sampled=100)
    assert report.disagreements == 1
    assert report.miss_rate == 0.01


def test_high_miss_rate_raises_an_alarm():
    findings = [
        AuditFinding(f"m{i}", TriageDecision.CLASSIFY_ONLY, 1, missed=True)
        for i in range(5)
    ]
    report = evaluate_audit(findings, sampled=100)
    assert audit_needs_attention(report) is not None


def test_acceptable_miss_rate_is_quiet():
    report = evaluate_audit(
        [AuditFinding("m1", TriageDecision.CLASSIFY_ONLY, 1, missed=True)], sampled=100
    )
    assert audit_needs_attention(report) is None


def test_small_samples_do_not_raise_alarms():
    findings = [
        AuditFinding(f"m{i}", TriageDecision.CLASSIFY_ONLY, 1, missed=True)
        for i in range(3)
    ]
    assert audit_needs_attention(evaluate_audit(findings, sampled=5)) is None


# ------------------------------------------------------- strong vs weak time

@pytest.mark.parametrize(
    "text", ["is due Friday", "deadline is Monday", "expires tomorrow", "final notice"]
)
def test_strong_temporal_language(text):
    assert has_strong_temporal_language(text)


@pytest.mark.parametrize(
    "text", ["top stories this week", "see you Tuesday", "our 5th anniversary"]
)
def test_weak_time_references_are_not_deadlines(text):
    """'This week' in a newsletter is descriptive, not an obligation."""
    assert not has_strong_temporal_language(text)
    assert has_temporal_language(text)


def test_transactional_and_bulk_are_treated_differently():
    """A role account writing to you is not the same as a mass mailing."""
    body = "Your package arrives Tuesday."
    transactional = triage(
        msg(body_clean=body, sender_email="notifications@shop.com", headers={})
    )
    mass = triage(
        msg(body_clean=body, sender_email="notifications@shop.com", headers=BULK_HEADERS)
    )
    assert transactional.decision is TriageDecision.EXTRACT
    assert mass.decision is TriageDecision.CLASSIFY_ONLY
