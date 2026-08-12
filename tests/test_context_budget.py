"""Context-window budgeting (adapted from Megamind's memory_manager)."""

from __future__ import annotations

import pytest

from backend.llm.base import ChatMessage
from backend.llm.context_budget import BudgetedItem, build_prompt, measure


async def counter(text: str) -> int:
    """One token per word — deterministic and easy to reason about."""
    return len(text.split())


def _history(n_turns: int, words: int = 10) -> list[BudgetedItem]:
    items = []
    for i in range(n_turns):
        role = "user" if i % 2 == 0 else "assistant"
        content = " ".join([f"w{i}"] * words)
        items.append(
            BudgetedItem(message=ChatMessage(role=role, content=content), tokens=words)
        )
    return items


async def test_short_conversation_is_untouched():
    result = await build_prompt(
        system_prompt="sys prompt here",
        history=_history(4),
        user_message="what is up",
        counter=counter,
        context_limit=4096,
        generation_buffer=256,
    )
    assert result.evicted_history == 0
    assert result.evicted_context == 0
    # system + 4 history + user
    assert len(result.messages) == 6
    assert result.messages[0].role == "system"
    assert result.messages[-1].content == "what is up"


async def test_history_is_evicted_when_over_budget():
    result = await build_prompt(
        system_prompt="sys",
        history=_history(20, words=50),
        user_message="hi",
        counter=counter,
        context_limit=600,
        generation_buffer=50,
    )
    assert result.evicted_history > 0
    assert result.used_tokens <= 600


async def test_retrieved_context_is_evicted_before_history():
    """A stale search result is worth less than the thread of conversation."""
    context = [
        BudgetedItem(
            message=ChatMessage(role="user", content=" ".join(["ctx"] * 100)),
            tokens=100,
            kind="context",
            ref=f"msg_{i}",
        )
        for i in range(5)
    ]
    result = await build_prompt(
        system_prompt="sys",
        history=_history(4, words=10),
        context_items=context,
        user_message="hi",
        counter=counter,
        context_limit=500,
        generation_buffer=50,
    )
    assert result.evicted_context > 0
    assert result.evicted_history == 0


async def test_system_prompt_and_question_are_never_evicted():
    result = await build_prompt(
        system_prompt=" ".join(["sys"] * 200),
        history=_history(10, words=100),
        user_message=" ".join(["q"] * 100),
        counter=counter,
        context_limit=400,
        generation_buffer=50,
    )
    assert result.messages[0].role == "system"
    assert result.messages[-1].role == "user"
    assert result.messages[-1].content.startswith("q")


async def test_impossible_budget_is_reported_not_silently_truncated():
    result = await build_prompt(
        system_prompt=" ".join(["sys"] * 500),
        history=[],
        user_message="hello",
        counter=counter,
        context_limit=300,
        generation_buffer=50,
    )
    assert any("exceed the usable window" in n for n in result.notes)


async def test_history_evicted_in_whole_turns():
    """The transcript must never begin mid-exchange with an assistant reply."""
    result = await build_prompt(
        system_prompt="sys",
        history=_history(10, words=40),
        user_message="hi",
        counter=counter,
        context_limit=400,
        generation_buffer=50,
    )
    remaining = [m for m in result.messages[1:-1]]
    if remaining:
        assert remaining[0].role == "user"


async def test_bigger_context_window_keeps_more_history():
    """The whole point of a configurable context length."""
    small = await build_prompt(
        system_prompt="sys",
        history=_history(30, words=50),
        user_message="hi",
        counter=counter,
        context_limit=1000,
        generation_buffer=100,
    )
    large = await build_prompt(
        system_prompt="sys",
        history=_history(30, words=50),
        user_message="hi",
        counter=counter,
        context_limit=32768,
        generation_buffer=100,
    )
    assert large.evicted_history == 0
    assert small.evicted_history > 0
    assert len(large.messages) > len(small.messages)


async def test_measure_counts_every_message():
    items = await measure(
        [ChatMessage(role="user", content="a b c"), ChatMessage(role="assistant", content="d e")],
        counter,
    )
    assert [i.tokens for i in items] == [3, 2]


async def test_utilisation_is_reported():
    result = await build_prompt(
        system_prompt="a b c",
        history=[],
        user_message="d e",
        counter=counter,
        context_limit=1000,
        generation_buffer=0,
    )
    assert result.used_tokens == 5
    assert 0 < result.utilisation < 1
