"""Ollama implementation of `LLMProvider`.

Talks to the REST API over httpx rather than the `ollama` python SDK, because
the SDK reads a process-global OLLAMA_HOST env var.  We need per-role base URLs
(chat on the desktop with the big GPU, embeddings on the laptop) and a plain
httpx client makes that trivial.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

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


def _to_wire_messages(messages: List[ChatMessage]) -> List[Dict[str, Any]]:
    wire: List[Dict[str, Any]] = []
    for m in messages:
        entry: Dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            entry["tool_calls"] = [
                {"function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in m.tool_calls
            ]
        if m.tool_name:
            entry["name"] = m.tool_name
        wire.append(entry)
    return wire


def _parse_tool_calls(payload: Dict[str, Any]) -> List[ToolCall]:
    raw = (payload.get("message") or {}).get("tool_calls") or []
    calls: List[ToolCall] = []
    for item in raw:
        fn = item.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        calls.append(ToolCall(name=name, arguments=args))
    return calls


def _usage_from(payload: Dict[str, Any]) -> Usage:
    return Usage(
        prompt_tokens=int(payload.get("prompt_eval_count") or 0),
        completion_tokens=int(payload.get("eval_count") or 0),
    )


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 300.0,
        keep_alive: str = "5m",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    # ------------------------------------------------------------------ core
    def _options(self, num_ctx: int, temperature: float, stop: Optional[List[str]]) -> Dict[str, Any]:
        opts: Dict[str, Any] = {"num_ctx": num_ctx, "temperature": temperature}
        if stop:
            opts["stop"] = stop
        return opts

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = await self._client.post(path, json=body)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:400]
            raise LLMUnavailableError(
                f"Ollama at {self.base_url} returned {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"Could not reach Ollama at {self.base_url}: {exc}"
            ) from exc

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
        body: Dict[str, Any] = {
            "model": model,
            "messages": _to_wire_messages(messages),
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": self._options(num_ctx, temperature, stop),
        }
        if json_schema is not None:
            body["format"] = json_schema

        payload = await self._post("/api/chat", body)
        return GenerationResult(
            text=(payload.get("message") or {}).get("content", ""),
            tool_calls=_parse_tool_calls(payload),
            usage=_usage_from(payload),
            model=model,
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
        body = {
            "model": model,
            "messages": _to_wire_messages(messages),
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": self._options(num_ctx, temperature, stop),
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:400]
                    raise LLMUnavailableError(
                        f"Ollama at {self.base_url} returned {resp.status_code}: {detail}"
                    )
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("error"):
                        raise LLMUnavailableError(str(payload["error"]))

                    delta = (payload.get("message") or {}).get("content", "") or ""
                    tool_calls = _parse_tool_calls(payload)
                    done = bool(payload.get("done"))
                    yield StreamChunk(
                        delta=delta,
                        done=done,
                        tool_calls=tool_calls,
                        usage=_usage_from(payload) if done else None,
                    )
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"Could not reach Ollama at {self.base_url}: {exc}"
            ) from exc

    async def tool_call(
        self,
        messages: List[ChatMessage],
        tools: List[ToolSpec],
        *,
        model: str,
        num_ctx: int,
        temperature: float = 0.0,
    ) -> GenerationResult:
        body = {
            "model": model,
            "messages": _to_wire_messages(messages),
            "tools": [t.to_wire() for t in tools],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": self._options(num_ctx, temperature, None),
        }
        payload = await self._post("/api/chat", body)
        return GenerationResult(
            text=(payload.get("message") or {}).get("content", ""),
            tool_calls=_parse_tool_calls(payload),
            usage=_usage_from(payload),
            model=model,
        )

    async def embed(self, texts: List[str], *, model: str) -> List[List[float]]:
        if not texts:
            return []
        payload = await self._post("/api/embed", {"model": model, "input": texts})
        return payload.get("embeddings", [])

    async def count_tokens(self, text: str, *, model: str) -> int:
        """Exact count via /api/tokenize, falling back to a chars/4 estimate.

        /api/tokenize is not present on every Ollama build, so a miss here is
        expected and must never be fatal.
        """
        if not text:
            return 0
        try:
            resp = await self._client.post(
                "/api/tokenize", json={"model": model, "prompt": text}, timeout=10.0
            )
            if resp.status_code == 200:
                return len(resp.json().get("tokens", []))
        except httpx.HTTPError:
            pass
        return max(1, len(text) // 4)

    async def list_models(self) -> List[str]:
        try:
            resp = await self._client.get("/api/tags", timeout=10.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"Could not reach Ollama at {self.base_url}: {exc}"
            ) from exc
        return [m.get("name", "") for m in resp.json().get("models", []) if m.get("name")]

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            version_resp = await self._client.get("/api/version", timeout=5.0)
            version_resp.raise_for_status()
            version = version_resp.json().get("version")
            models = await self.list_models()
        except (httpx.HTTPError, LLMUnavailableError) as exc:
            return ProviderHealth(
                connected=False,
                base_url=self.base_url,
                error=_friendly_error(self.base_url, exc),
            )
        return ProviderHealth(
            connected=True,
            base_url=self.base_url,
            version=version,
            models=models,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _friendly_error(base_url: str, exc: Exception) -> str:
    """Turn connection errors into something actionable on the status page."""
    text = str(exc)
    if isinstance(exc, httpx.ConnectError) or "Connection refused" in text:
        host = httpx.URL(base_url).host or base_url
        if host in ("localhost", "127.0.0.1", "::1"):
            return f"Nothing is listening at {base_url}. Is `ollama serve` running?"
        return (
            f"Nothing is listening at {base_url}. On that machine, start Ollama with "
            "`OLLAMA_HOST=0.0.0.0 ollama serve` and check the firewall."
        )
    if isinstance(exc, httpx.ConnectTimeout):
        return f"Timed out connecting to {base_url}. Wrong IP, or a firewall is dropping packets."
    return f"{base_url}: {text}"
