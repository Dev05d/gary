"""Deterministic stand-ins so tests never need Ollama or a real account."""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from backend.llm.base import (
    ChatMessage,
    GenerationResult,
    LLMProvider,
    LLMUnavailableError,
    ProviderHealth,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
)


class FakeProvider(LLMProvider):
    """Echoes a scripted reply and records exactly what it was asked."""

    name = "fake"

    def __init__(
        self,
        reply: str = "Hello from the fake model.",
        *,
        base_url: str = "http://fake-ollama:11434",
        installed: Optional[List[str]] = None,
        fail_with: Optional[Exception] = None,
    ) -> None:
        self.reply = reply
        self.base_url = base_url
        self.installed = installed if installed is not None else ["fake-large", "fake-fast"]
        self.fail_with = fail_with
        self.calls: List[Dict[str, Any]] = []
        self.tool_response: List[ToolCall] = []

    def _record(self, kind: str, messages: List[ChatMessage], **kw: Any) -> None:
        self.calls.append({"kind": kind, "messages": list(messages), **kw})

    async def generate(
        self,
        messages: List[ChatMessage],
        *,
        model: str,
        num_ctx: int,
        temperature: float = 0.3,
        json_schema: Optional[Dict[str, Any]] = None,
        stop: Optional[List[str]] = None,
    ) -> GenerationResult:
        if self.fail_with:
            raise self.fail_with
        self._record("generate", messages, model=model, num_ctx=num_ctx)
        return GenerationResult(
            text=self.reply, usage=Usage(prompt_tokens=10, completion_tokens=5), model=model
        )

    async def stream(
        self,
        messages: List[ChatMessage],
        *,
        model: str,
        num_ctx: int,
        temperature: float = 0.3,
        stop: Optional[List[str]] = None,
    ) -> AsyncIterator[StreamChunk]:
        if self.fail_with:
            raise self.fail_with
        self._record("stream", messages, model=model, num_ctx=num_ctx)
        for word in self.reply.split(" "):
            yield StreamChunk(delta=word + " ")
        yield StreamChunk(done=True, usage=Usage(prompt_tokens=10, completion_tokens=5))

    async def tool_call(
        self,
        messages: List[ChatMessage],
        tools: List[ToolSpec],
        *,
        model: str,
        num_ctx: int,
        temperature: float = 0.0,
    ) -> GenerationResult:
        if self.fail_with:
            raise self.fail_with
        self._record("tool_call", messages, tools=[t.name for t in tools], model=model)
        return GenerationResult(text="", tool_calls=list(self.tool_response), model=model)

    async def embed(self, texts: List[str], *, model: str) -> List[List[float]]:
        return [[float(len(t)), 0.5, 0.25] for t in texts]

    async def count_tokens(self, text: str, *, model: str) -> int:
        # Stable and cheap: one token per whitespace-separated word.
        return len(text.split())

    async def list_models(self) -> List[str]:
        return list(self.installed)

    async def health(self) -> ProviderHealth:
        if self.fail_with:
            return ProviderHealth(
                connected=False, base_url=self.base_url, error=str(self.fail_with)
            )
        return ProviderHealth(
            connected=True,
            base_url=self.base_url,
            version="fake-0.0.0",
            models=list(self.installed),
            latency_ms=1.0,
        )


class UnreachableProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__(fail_with=LLMUnavailableError("Could not reach Ollama at http://nope"))
