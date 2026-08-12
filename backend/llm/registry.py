"""Maps a *task role* to a concrete (provider, model, context window).

Callers ask for "the fast model" or "the large model", never for a model name.
That is what makes §25 of the spec (small model for classification, big model
for reasoning) a config change rather than a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from backend.config import ModelRole, Settings
from backend.llm.base import LLMProvider
from backend.llm.ollama_provider import OllamaProvider


@dataclass(frozen=True)
class ModelBinding:
    role: ModelRole
    provider: LLMProvider
    model: str
    num_ctx: int

    @property
    def base_url(self) -> str:
        return getattr(self.provider, "base_url", "")


class LLMRegistry:
    """Owns provider lifetimes and hands out role bindings.

    One provider instance (and therefore one httpx connection pool) per base
    URL, shared across roles that point at the same host.
    """

    def __init__(self, settings: Settings, provider_factory=None) -> None:
        self._settings = settings
        self._factory = provider_factory or self._default_factory
        self._providers: Dict[str, LLMProvider] = {}

    def _default_factory(self, base_url: str) -> LLMProvider:
        return OllamaProvider(
            base_url,
            timeout=self._settings.ollama_timeout,
            keep_alive=self._settings.ollama_keep_alive,
        )

    def provider_for_url(self, base_url: str) -> LLMProvider:
        if base_url not in self._providers:
            self._providers[base_url] = self._factory(base_url)
        return self._providers[base_url]

    def binding(self, role: ModelRole) -> ModelBinding:
        s = self._settings
        return ModelBinding(
            role=role,
            provider=self.provider_for_url(s.base_url_for(role)),
            model=s.model_for(role),
            num_ctx=s.context_for(role),
        )

    @property
    def distinct_base_urls(self) -> Dict[str, LLMProvider]:
        """Every backend we might talk to — used by the status page."""
        s = self._settings
        for url in {s.ollama_base_url, s.embed_base_url}:
            self.provider_for_url(url)
        return dict(self._providers)

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
        self._providers.clear()


_registry: Optional[LLMRegistry] = None


def get_registry(settings: Optional[Settings] = None) -> LLMRegistry:
    global _registry
    if _registry is None:
        if settings is None:
            from backend.config import get_settings

            settings = get_settings()
        _registry = LLMRegistry(settings)
    return _registry


def set_registry(registry: Optional[LLMRegistry]) -> None:
    """Test/lifespan hook for swapping in a fake provider."""
    global _registry
    _registry = registry
