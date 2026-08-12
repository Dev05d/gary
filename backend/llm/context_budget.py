"""Keeps a prompt inside the model's context window.

Ported from the cascading token manager in Dev05d/Megamind (`core/memory_manager.py`),
adapted to async token counting and this app's message shape.

Eviction order is deliberate and matches Megamind's: retrieved context is
dropped before conversation history, because a stale search result is less
valuable than the thread of the conversation.  In M3 the evicted retrieval
chunks become re-fetchable, so dropping them is cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence

from backend.llm.base import ChatMessage

SAFETY_MARGIN = 200


class TokenCounter(Protocol):
    async def __call__(self, text: str) -> int: ...


@dataclass
class BudgetedItem:
    """A message plus its measured token cost."""

    message: ChatMessage
    tokens: int
    # Retrieved-context items are evicted first; conversation turns second.
    kind: str = "history"  # "history" | "context"
    ref: Optional[str] = None  # source id, for logging / citation


@dataclass
class BudgetResult:
    messages: List[ChatMessage]
    used_tokens: int
    limit: int
    evicted_context: int = 0
    evicted_history: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def utilisation(self) -> float:
        return round(self.used_tokens / self.limit, 3) if self.limit else 0.0


async def measure(
    messages: Sequence[ChatMessage],
    counter: TokenCounter,
    *,
    kind: str = "history",
) -> List[BudgetedItem]:
    items: List[BudgetedItem] = []
    for m in messages:
        items.append(BudgetedItem(message=m, tokens=await counter(m.content), kind=kind))
    return items


async def build_prompt(
    *,
    system_prompt: str,
    history: List[BudgetedItem],
    context_items: Optional[List[BudgetedItem]] = None,
    user_message: str,
    counter: TokenCounter,
    context_limit: int,
    generation_buffer: int = 2048,
    safety_margin: int = SAFETY_MARGIN,
) -> BudgetResult:
    """Assemble the final message list, evicting until it fits.

    Never evicts the system prompt or the current user message — if those alone
    blow the budget the caller has a configuration problem and should hear
    about it rather than silently get a truncated prompt.
    """
    context_items = list(context_items or [])
    history = list(history)

    system_tokens = await counter(system_prompt)
    user_tokens = await counter(user_message)

    target = context_limit - safety_margin - generation_buffer
    floor = system_tokens + user_tokens

    notes: List[str] = []
    if floor > target:
        notes.append(
            f"System prompt + question ({floor} tokens) exceed the usable window "
            f"({target}). Raise LLM_CONTEXT_* or shorten the question."
        )

    def total() -> int:
        return floor + sum(i.tokens for i in context_items) + sum(i.tokens for i in history)

    evicted_context = 0
    evicted_history = 0

    # 1. Shed retrieved context (oldest / least relevant first).
    while total() > target and context_items:
        dropped = context_items.pop(0)
        evicted_context += 1
        notes.append(f"Evicted context {dropped.ref or '?'} (-{dropped.tokens} tokens)")

    # 2. Then shed conversation history, in whole user+assistant turns so the
    #    transcript never starts mid-exchange.
    while total() > target and len(history) >= 2:
        a = history.pop(0)
        b = history.pop(0)
        evicted_history += 2
        notes.append(f"Evicted an older turn (-{a.tokens + b.tokens} tokens)")

    while total() > target and history:
        dropped = history.pop(0)
        evicted_history += 1
        notes.append(f"Evicted an older message (-{dropped.tokens} tokens)")

    messages: List[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]
    messages.extend(i.message for i in context_items)
    messages.extend(i.message for i in history)
    messages.append(ChatMessage(role="user", content=user_message))

    return BudgetResult(
        messages=messages,
        used_tokens=total(),
        limit=context_limit,
        evicted_context=evicted_context,
        evicted_history=evicted_history,
        notes=notes,
    )
