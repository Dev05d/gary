"""Importance scoring: model judgement blended with your own behaviour.

A fresh LLM cannot know that your advisor outranks a newsletter. It reads
content, so it rewards messages that *look* urgent — which is exactly what
marketing is engineered to do. Meanwhile the strongest available signal is
sitting in the database already: how you have actually treated this sender.

    You reply to your advisor in 20 minutes.
    You have never replied to notifications@ anything.

That is a far better prior than any amount of reading the text, and it is
computed from data Gary already holds.

The blend is confidence-weighted so it degrades gracefully. With no history the
score is pure model output; as observations accumulate the prior earns
influence, capped so behaviour can shade a judgement but never override an
obviously-urgent message from someone new.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional


# --------------------------------------------------------------------------
# Behavioural prior
# --------------------------------------------------------------------------

@dataclass
class SenderBehaviour:
    """Observed history for one sender. All counts are over messages received."""

    messages_received: int = 0
    messages_replied_to: int = 0
    #: Median time from their message to your reply, over replied messages.
    median_reply_latency: Optional[timedelta] = None
    times_starred: int = 0
    times_archived_unread: int = 0
    times_opened: int = 0

    @property
    def reply_rate(self) -> float:
        if self.messages_received <= 0:
            return 0.0
        return min(1.0, self.messages_replied_to / self.messages_received)

    @property
    def star_rate(self) -> float:
        if self.messages_received <= 0:
            return 0.0
        return min(1.0, self.times_starred / self.messages_received)

    @property
    def ignore_rate(self) -> float:
        """Archived without ever being opened — the clearest 'I don't care'."""
        if self.messages_received <= 0:
            return 0.0
        return min(1.0, self.times_archived_unread / self.messages_received)


#: Observations needed before the prior carries its full weight. Below this it
#: is scaled down proportionally, so a single fast reply cannot mint a VIP.
WARMUP_OBSERVATIONS = 8

#: Ceiling on how much behaviour may move the final score. Behaviour should
#: shade the model's judgement, not replace it — a genuinely urgent first
#: message from a stranger must still be able to score high.
MAX_PRIOR_WEIGHT = 0.45

#: Reply latency at or below this reads as "I drop things for this person".
FAST_REPLY = timedelta(hours=2)
#: Latency beyond this carries no positive signal.
SLOW_REPLY = timedelta(days=3)


def latency_score(latency: Optional[timedelta]) -> float:
    """1.0 for a near-instant reply, decaying to 0.0 over three days."""
    if latency is None:
        return 0.0
    seconds = latency.total_seconds()
    if seconds <= FAST_REPLY.total_seconds():
        return 1.0
    if seconds >= SLOW_REPLY.total_seconds():
        return 0.0
    span = SLOW_REPLY.total_seconds() - FAST_REPLY.total_seconds()
    return 1.0 - (seconds - FAST_REPLY.total_seconds()) / span


def prior_score(behaviour: SenderBehaviour) -> float:
    """How much you appear to care about this sender, 0–1.

    Reply rate dominates: replying is the least ambiguous signal a person
    emits. Starring is strong but rare. Ignoring is subtracted rather than
    treated as a separate axis, so a sender you reliably bin lands near zero
    even if you occasionally opened one.
    """
    # Starring is weighted low deliberately: even people who matter rarely get
    # starred, so a high weight here would drag every genuine VIP down.
    positive = (
        0.65 * behaviour.reply_rate
        + 0.25 * latency_score(behaviour.median_reply_latency)
        + 0.10 * behaviour.star_rate
    )
    return max(0.0, min(1.0, positive - 0.35 * behaviour.ignore_rate))


def prior_strength(behaviour: SenderBehaviour) -> float:
    """How much the prior should be trusted, 0–1, from sample size alone."""
    if behaviour.messages_received <= 0:
        return 0.0
    return min(1.0, behaviour.messages_received / WARMUP_OBSERVATIONS)


@dataclass
class ImportanceResult:
    score: float
    model_score: float
    prior: Optional[float]
    weight: float
    explanation: str

    @property
    def is_cold_start(self) -> bool:
        return self.weight == 0.0


def score_importance(
    model_score: float,
    behaviour: Optional[SenderBehaviour] = None,
    *,
    is_automated: bool = False,
    max_prior_weight: float = MAX_PRIOR_WEIGHT,
) -> ImportanceResult:
    """Blend the model's judgement with the sender prior.

    `is_automated` caps the result: a newsletter is never important, however
    urgent its copy, and however often you happen to open it.
    """
    model_score = max(0.0, min(1.0, model_score))

    if behaviour is None or behaviour.messages_received <= 0:
        score = model_score
        result = ImportanceResult(
            score=score,
            model_score=model_score,
            prior=None,
            weight=0.0,
            explanation="No history with this sender yet — model judgement only.",
        )
    else:
        prior = prior_score(behaviour)
        weight = max_prior_weight * prior_strength(behaviour)
        score = (1 - weight) * model_score + weight * prior

        direction = "raised" if prior > model_score else "lowered"
        result = ImportanceResult(
            score=round(score, 4),
            model_score=model_score,
            prior=round(prior, 4),
            weight=round(weight, 4),
            explanation=(
                f"Model scored {model_score:.2f}; your history with this sender "
                f"({behaviour.messages_replied_to}/{behaviour.messages_received} replied) "
                f"{direction} it to {score:.2f}."
            ),
        )

    if is_automated:
        capped = min(result.score, AUTOMATED_CEILING)
        if capped < result.score:
            result = ImportanceResult(
                score=round(capped, 4),
                model_score=result.model_score,
                prior=result.prior,
                weight=result.weight,
                explanation=result.explanation
                + f" Capped at {AUTOMATED_CEILING} because the message is automated.",
            )

    return result


#: Automated mail cannot exceed this, whatever the model or the prior say.
#: Bills still surface — via their extracted deadline, not via an alert.
AUTOMATED_CEILING = 0.5
