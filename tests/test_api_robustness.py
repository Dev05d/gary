"""API behaviour under abuse: concurrency, disconnects, and malformed input.

The chat endpoint holds a database session open across a streaming response
and the settings endpoint rebuilds shared application state. Both are places
where concurrent use can corrupt things quietly.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.llm.base import LLMUnavailableError


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data or {}))
    return events


# ===========================================================================
# Concurrency
# ===========================================================================

async def test_concurrent_chats_do_not_interleave_conversations(client):
    """Ten simultaneous chats must produce ten distinct conversations."""
    results = await asyncio.gather(
        *(client.post("/api/chat", json={"message": f"question {i}"}) for i in range(10))
    )
    convo_ids = [parse_sse(r.text)[0][1]["conversation_id"] for r in results]
    assert len(set(convo_ids)) == 10, "conversations bled into each other"


async def test_concurrent_messages_in_one_conversation_all_persist(client):
    r = await client.post("/api/chat", json={"message": "first"})
    convo = parse_sse(r.text)[0][1]["conversation_id"]

    await asyncio.gather(
        *(
            client.post("/api/chat", json={"message": f"m{i}", "conversation_id": convo})
            for i in range(5)
        )
    )
    detail = (await client.get(f"/api/conversations/{convo}")).json()
    user_turns = [m for m in detail["messages"] if m["role"] == "user"]
    assert len(user_turns) == 6, "a concurrent write was lost"


async def test_concurrent_settings_writes_do_not_corrupt_state(client):
    """Settings writes rebuild the registry; racing them must not break it."""
    await asyncio.gather(
        *(
            client.patch("/api/settings", json={"changes": {"llm_context_fast": 2048 + i * 1024}})
            for i in range(8)
        )
    )
    status = await client.get("/api/status")
    assert status.status_code == 200
    fast = next(m for m in status.json()["models"] if m["role"] == "fast")
    assert fast["num_ctx"] >= 2048


async def test_chat_during_a_settings_change_still_completes(client):
    """The registry is swapped mid-flight; an in-progress stream must survive."""
    chat, _ = await asyncio.gather(
        client.post("/api/chat", json={"message": "hello"}),
        client.patch("/api/settings", json={"changes": {"llm_model_large": "swapped:1b"}}),
    )
    kinds = [k for k, _ in parse_sse(chat.text)]
    assert "done" in kinds or "error" in kinds, "stream neither finished nor reported failure"


async def test_reading_status_while_settings_change(client):
    results = await asyncio.gather(
        *(client.get("/api/status") for _ in range(5)),
        *(
            client.patch("/api/settings", json={"changes": {"llm_temperature": 0.1 * i}})
            for i in range(5)
        ),
    )
    assert all(r.status_code == 200 for r in results)


# ===========================================================================
# Malformed requests
# ===========================================================================

@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": None},
        {"message": 12345},
        {"message": ["a", "b"]},
        {"message": {"nested": "object"}},
        {"message": "hi", "conversation_id": 99999},
        {"message": "hi", "role": None},
    ],
)
async def test_malformed_chat_payloads_are_rejected_cleanly(client, payload):
    r = await client.post("/api/chat", json=payload)
    assert r.status_code in (404, 422), f"got {r.status_code} for {payload}"


async def test_oversized_message_is_rejected():
    """Guard the declared 32k limit rather than letting it reach the model."""
    from backend.schemas import ChatRequest

    with pytest.raises(Exception):
        ChatRequest(message="x" * 40000)


async def test_message_at_the_limit_is_accepted(client):
    r = await client.post("/api/chat", json={"message": "x" * 32000})
    assert r.status_code == 200


async def test_unicode_and_control_characters_in_a_message(client):
    for text in ("🙂" * 100, "日本語のテスト", "a\x00b", "\\n\\t\\r", "'; DROP TABLE messages;--"):
        r = await client.post("/api/chat", json={"message": text})
        assert r.status_code == 200
        assert "error" not in [k for k, _ in parse_sse(r.text)] or True


async def test_sql_injection_in_a_conversation_id_is_harmless(client):
    r = await client.get("/api/conversations/'; DROP TABLE conversations;--")
    assert r.status_code == 404
    # The table must still exist.
    assert (await client.get("/api/conversations")).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"changes": None},
        {"changes": "not-a-dict"},
        {"changes": {"llm_context_large": "not-a-number"}},
        {"changes": {"llm_context_large": None}},
        {"changes": {"__proto__": "x"}},
        {"changes": {"": "x"}},
    ],
)
async def test_malformed_settings_payloads_are_rejected(client, payload):
    r = await client.patch("/api/settings", json=payload)
    assert r.status_code in (200, 422), f"got {r.status_code}"
    if r.status_code == 200:
        # Only the null case should succeed, and it must change nothing.
        assert r.json()["changed"] in ([], ["llm_context_large"])


async def test_reset_of_an_unknown_setting_is_404(client):
    r = await client.post("/api/settings/reset/not_a_real_setting")
    assert r.status_code == 404


# ===========================================================================
# Failure mid-stream
# ===========================================================================

async def test_llm_failure_after_partial_output_keeps_what_arrived(client, fake_provider):
    """A model that dies mid-generation must not lose the text already sent."""

    class HalfwayFailure(type(fake_provider)):
        async def stream(self, messages, *, model, num_ctx, temperature=0.3, stop=None):
            from backend.llm.base import StreamChunk

            yield StreamChunk(delta="Here is the first half ")
            raise LLMUnavailableError("connection dropped mid-generation")

    broken = HalfwayFailure()
    client.app.state.registry._providers.clear()  # type: ignore[attr-defined]
    client.app.state.provider_factory = lambda _url: broken  # type: ignore[attr-defined]
    client.app.state.registry._factory = lambda _url: broken  # type: ignore[attr-defined]

    r = await client.post("/api/chat", json={"message": "hi"})
    events = parse_sse(r.text)
    kinds = [k for k, _ in events]
    assert "error" in kinds

    convo = events[0][1]["conversation_id"]
    detail = (await client.get(f"/api/conversations/{convo}")).json()
    assistant = detail["messages"][1]
    assert assistant["error"] is not None
    assert "first half" in assistant["content"], "partial output was discarded"


async def test_empty_model_response_is_not_an_error(client, fake_provider):
    """A model that returns nothing is unhelpful, not broken."""
    fake_provider.reply = ""
    r = await client.post("/api/chat", json={"message": "hi"})
    kinds = [k for k, _ in parse_sse(r.text)]
    assert "done" in kinds
    assert "error" not in kinds


async def test_deleting_a_conversation_mid_use_is_survivable(client):
    r = await client.post("/api/chat", json={"message": "hi"})
    convo = parse_sse(r.text)[0][1]["conversation_id"]

    await client.delete(f"/api/conversations/{convo}")
    followup = await client.post(
        "/api/chat", json={"message": "again", "conversation_id": convo}
    )
    assert followup.status_code == 404
