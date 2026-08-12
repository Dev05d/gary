"""Importance: model judgement blended with observed behaviour."""

from __future__ import annotations

from datetime import timedelta

from backend.pipeline.importance import (
    AUTOMATED_CEILING,
    SenderBehaviour,
    latency_score,
    prior_score,
    prior_strength,
    score_importance,
)


def advisor() -> SenderBehaviour:
    """Someone you drop everything for."""
    return SenderBehaviour(
        messages_received=40,
        messages_replied_to=36,
        median_reply_latency=timedelta(minutes=25),
        times_starred=8,
        times_opened=40,
    )


def ignored_sender() -> SenderBehaviour:
    return SenderBehaviour(
        messages_received=60,
        messages_replied_to=0,
        median_reply_latency=None,
        times_archived_unread=55,
    )


# ------------------------------------------------------------------- pieces

def test_fast_replies_score_high():
    assert latency_score(timedelta(minutes=10)) == 1.0
    assert latency_score(timedelta(hours=2)) == 1.0


def test_slow_replies_score_zero():
    assert latency_score(timedelta(days=5)) == 0.0


def test_latency_decays_in_between():
    mid = latency_score(timedelta(hours=24))
    assert 0.0 < mid < 1.0


def test_never_replied_has_no_latency_signal():
    assert latency_score(None) == 0.0


def test_prior_ranks_advisor_far_above_ignored_sender():
    assert prior_score(advisor()) > 0.8
    assert prior_score(ignored_sender()) < 0.1


def test_prior_strength_grows_with_evidence():
    assert prior_strength(SenderBehaviour(messages_received=0)) == 0.0
    assert prior_strength(SenderBehaviour(messages_received=4)) == 0.5
    assert prior_strength(SenderBehaviour(messages_received=8)) == 1.0
    assert prior_strength(SenderBehaviour(messages_received=500)) == 1.0


# ------------------------------------------------------------------- blending

def test_cold_start_is_pure_model_score():
    result = score_importance(0.7, None)
    assert result.score == 0.7
    assert result.is_cold_start
    assert "No history" in result.explanation


def test_a_single_message_barely_moves_the_score():
    """One fast reply must not mint a VIP."""
    one = SenderBehaviour(
        messages_received=1, messages_replied_to=1, median_reply_latency=timedelta(minutes=5)
    )
    result = score_importance(0.3, one)
    assert result.weight < 0.1
    assert abs(result.score - 0.3) < 0.08


def test_advisor_gets_a_quiet_note_promoted():
    """The model sees unremarkable text; your behaviour says it matters."""
    plain = score_importance(0.35, None).score
    with_history = score_importance(0.35, advisor()).score
    assert with_history > plain
    assert with_history > 0.5


def test_ignored_sender_gets_urgent_copy_demoted():
    """The model is fooled by 'URGENT'; sixty ignored messages are not."""
    fooled = score_importance(0.85, None).score
    with_history = score_importance(0.85, ignored_sender()).score
    assert with_history < fooled
    assert with_history < 0.55


def test_behaviour_can_shade_but_never_override():
    """A genuinely urgent first message from a stranger must still land."""
    result = score_importance(0.95, ignored_sender())
    assert result.weight <= 0.45
    assert result.score >= 0.5, "a 0.95 model score must not be buried"


def test_automated_mail_is_capped():
    result = score_importance(0.95, advisor(), is_automated=True)
    assert result.score <= AUTOMATED_CEILING
    assert "Capped" in result.explanation


def test_cap_does_not_raise_a_low_score():
    result = score_importance(0.2, None, is_automated=True)
    assert result.score == 0.2
    assert "Capped" not in result.explanation


def test_scores_stay_in_range():
    for model in (0.0, 0.5, 1.0):
        for behaviour in (None, advisor(), ignored_sender()):
            score = score_importance(model, behaviour).score
            assert 0.0 <= score <= 1.0


def test_out_of_range_model_scores_are_clamped():
    assert score_importance(1.7, None).score == 1.0
    assert score_importance(-0.4, None).score == 0.0


def test_explanation_names_the_evidence():
    result = score_importance(0.4, advisor())
    assert "36/40 replied" in result.explanation
