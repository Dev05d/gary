"""Provider-agnostic LLM interface.

Nothing above this layer may import `httpx`, know about Ollama, or hard-code a
model name.  Swapping in llama.cpp / vLLM / LM Studio means writing one new
subclass of `LLMProvider` and registering it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    content: str
    # Populated on assistant turns that requested tools, and on tool results.
    tool_calls: Optional[List["ToolCall"]] = None
    tool_name: Optional[str] = None


class ToolCall(BaseModel):
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    """JSON-schema description of a callable tool, in OpenAI/Ollama shape."""

    name: str
    description: str
    parameters: Dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    def to_wire(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class StreamChunk:
    """One increment of a streamed response."""

    delta: str = ""
    done: bool = False
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Optional[Usage] = None


@dataclass
class GenerationResult:
    text: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""


class LLMUnavailableError(RuntimeError):
    """The inference backend could not be reached or refused the request."""


class LLMProvider(ABC):
    """Every backend implements this and nothing more."""

    name: str = "abstract"

    @abstractmethod
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
        """Single-shot completion. `json_schema` requests structured output."""

    @abstractmethod
    def stream(
        self,
        messages: List[ChatMessage],
        *,
        model: str,
        num_ctx: int,
        temperature: float = 0.3,
        stop: Optional[List[str]] = None,
    ) -> AsyncIterator[StreamChunk]:
        """Token-by-token completion."""

    @abstractmethod
    async def tool_call(
        self,
        messages: List[ChatMessage],
        tools: List[ToolSpec],
        *,
        model: str,
        num_ctx: int,
        temperature: float = 0.0,
    ) -> GenerationResult:
        """Completion where the model may request one or more tools."""

    @abstractmethod
    async def embed(self, texts: List[str], *, model: str) -> List[List[float]]:
        """Dense embeddings, one vector per input string."""

    @abstractmethod
    async def count_tokens(self, text: str, *, model: str) -> int:
        """Exact token count where the backend exposes it, else an estimate."""

    @abstractmethod
    async def list_models(self) -> List[str]:
        """Model tags available on the backend."""

    @abstractmethod
    async def health(self) -> "ProviderHealth":
        """Cheap reachability probe for the status page."""

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        return None


class ProviderHealth(BaseModel):
    connected: bool
    base_url: str
    version: Optional[str] = None
    models: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    latency_ms: Optional[float] = None


ChatMessage.model_rebuild()
