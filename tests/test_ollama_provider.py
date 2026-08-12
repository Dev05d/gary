"""OllamaProvider wire-format tests against a mock transport (no real Ollama)."""

from __future__ import annotations

import json

import httpx
import pytest

from backend.llm.base import ChatMessage, LLMUnavailableError, ToolSpec
from backend.llm.ollama_provider import OllamaProvider

BASE = "http://192.168.1.42:11434"


def provider_with(handler) -> OllamaProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url=BASE)
    return OllamaProvider(BASE, client=client)


def ndjson(*objs) -> str:
    return "\n".join(json.dumps(o) for o in objs)


async def test_generate_posts_expected_body_and_parses_reply():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "hi there"},
                "prompt_eval_count": 12,
                "eval_count": 3,
            },
        )

    result = await provider_with(handler).generate(
        [ChatMessage(role="user", content="hello")],
        model="gemma3:27b",
        num_ctx=32768,
        temperature=0.2,
    )

    assert captured["url"] == f"{BASE}/api/chat"
    assert captured["body"]["model"] == "gemma3:27b"
    assert captured["body"]["stream"] is False
    assert captured["body"]["options"]["num_ctx"] == 32768
    assert captured["body"]["options"]["temperature"] == 0.2
    assert result.text == "hi there"
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 3


async def test_context_length_is_actually_sent_to_ollama():
    """num_ctx is the whole reason long conversations work; assert it explicitly."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "ok"}})

    await provider_with(handler).generate(
        [ChatMessage(role="user", content="x")], model="m", num_ctx=131072
    )
    assert captured["options"]["num_ctx"] == 131072


async def test_stream_yields_deltas_then_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=ndjson(
                {"message": {"content": "Hello"}, "done": False},
                {"message": {"content": " world"}, "done": False},
                {"message": {"content": ""}, "done": True, "prompt_eval_count": 5, "eval_count": 2},
            ),
        )

    chunks = [
        c
        async for c in provider_with(handler).stream(
            [ChatMessage(role="user", content="hi")], model="m", num_ctx=8192
        )
    ]
    assert "".join(c.delta for c in chunks) == "Hello world"
    assert chunks[-1].done is True
    assert chunks[-1].usage.completion_tokens == 2


async def test_stream_ignores_malformed_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="not json\n" + ndjson({"message": {"content": "ok"}, "done": True}),
        )

    chunks = [
        c
        async for c in provider_with(handler).stream(
            [ChatMessage(role="user", content="hi")], model="m", num_ctx=8192
        )
    ]
    assert "".join(c.delta for c in chunks) == "ok"


async def test_tool_calls_are_parsed_including_string_arguments():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "search_messages", "arguments": {"q": "internship"}}},
                        {"function": {"name": "get_message", "arguments": '{"id": "m1"}'}},
                    ],
                }
            },
        )

    result = await provider_with(handler).tool_call(
        [ChatMessage(role="user", content="find it")],
        [ToolSpec(name="search_messages", description="search")],
        model="m",
        num_ctx=8192,
    )
    assert [tc.name for tc in result.tool_calls] == ["search_messages", "get_message"]
    assert result.tool_calls[0].arguments == {"q": "internship"}
    assert result.tool_calls[1].arguments == {"id": "m1"}


async def test_tools_are_sent_in_openai_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": ""}})

    await provider_with(handler).tool_call(
        [ChatMessage(role="user", content="x")],
        [ToolSpec(name="search_messages", description="Search email")],
        model="m",
        num_ctx=8192,
    )
    tool = captured["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "search_messages"


async def test_embed_returns_one_vector_per_input():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]] * len(payload["input"])})

    vectors = await provider_with(handler).embed(["a", "b", "c"], model="embed")
    assert len(vectors) == 3


async def test_count_tokens_uses_tokenize_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tokenize"
        return httpx.Response(200, json={"tokens": [1, 2, 3, 4, 5]})

    assert await provider_with(handler).count_tokens("hello", model="m") == 5


async def test_count_tokens_falls_back_when_endpoint_missing():
    """/api/tokenize is absent on some builds — must degrade, not crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    n = await provider_with(handler).count_tokens("a" * 400, model="m")
    assert n == 100  # chars // 4


async def test_connection_error_names_the_remote_host_and_the_fix():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    health = await provider_with(handler).health()
    assert health.connected is False
    assert BASE in health.error
    assert "OLLAMA_HOST=0.0.0.0" in health.error


async def test_localhost_connection_error_suggests_ollama_serve():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://localhost:11434")
    provider = OllamaProvider("http://localhost:11434", client=client)
    health = await provider.health()
    assert "ollama serve" in health.error


async def test_http_error_is_wrapped_not_leaked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model not found")

    with pytest.raises(LLMUnavailableError, match="500"):
        await provider_with(handler).generate(
            [ChatMessage(role="user", content="x")], model="m", num_ctx=1024
        )


async def test_health_lists_installed_models():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.1"})
        return httpx.Response(200, json={"models": [{"name": "gemma3:27b"}, {"name": "qwen3:8b"}]})

    health = await provider_with(handler).health()
    assert health.connected is True
    assert health.version == "0.5.1"
    assert health.models == ["gemma3:27b", "qwen3:8b"]
