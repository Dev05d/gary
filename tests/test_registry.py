"""Model-role routing and provider reuse (spec §25)."""

from __future__ import annotations

from backend.config import Settings
from backend.llm.registry import LLMRegistry
from tests.fakes import FakeProvider


def _settings(**kw) -> Settings:
    kw.setdefault("_env_file", None)
    return Settings(**kw)


def test_each_role_resolves_to_its_configured_model():
    s = _settings(
        llm_model_large="big:70b",
        llm_model_fast="small:8b",
        llm_model_router="small:8b",
        embedding_model="embed:1b",
    )
    reg = LLMRegistry(s, provider_factory=lambda url: FakeProvider(base_url=url))
    assert reg.binding("large").model == "big:70b"
    assert reg.binding("fast").model == "small:8b"
    assert reg.binding("embed").model == "embed:1b"


def test_each_role_gets_its_own_context_window():
    s = _settings(llm_context_large=65536, llm_context_fast=4096)
    reg = LLMRegistry(s, provider_factory=lambda url: FakeProvider(base_url=url))
    assert reg.binding("large").num_ctx == 65536
    assert reg.binding("fast").num_ctx == 4096


def test_one_provider_instance_is_shared_per_base_url():
    """Roles on the same host must share a connection pool."""
    created = []

    def factory(url: str) -> FakeProvider:
        created.append(url)
        return FakeProvider(base_url=url)

    s = _settings(ollama_base_url="http://desktop:11434")
    reg = LLMRegistry(s, provider_factory=factory)
    reg.binding("large")
    reg.binding("fast")
    reg.binding("embed")
    assert created == ["http://desktop:11434"]


def test_split_hosts_create_two_providers():
    created = []

    def factory(url: str) -> FakeProvider:
        created.append(url)
        return FakeProvider(base_url=url)

    s = _settings(
        ollama_base_url="http://desktop:11434",
        ollama_embed_base_url="http://laptop:11434",
    )
    reg = LLMRegistry(s, provider_factory=factory)
    reg.binding("large")
    reg.binding("embed")
    assert sorted(created) == ["http://desktop:11434", "http://laptop:11434"]
    assert reg.binding("embed").base_url == "http://laptop:11434"


def test_distinct_base_urls_covers_every_configured_host():
    s = _settings(
        ollama_base_url="http://a:11434", ollama_embed_base_url="http://b:11434"
    )
    reg = LLMRegistry(s, provider_factory=lambda url: FakeProvider(base_url=url))
    assert set(reg.distinct_base_urls) == {"http://a:11434", "http://b:11434"}


def test_swapping_models_needs_no_code_change():
    """The point of the registry: config in, different model out."""
    reg = LLMRegistry(
        _settings(llm_model_large="qwen3:32b"),
        provider_factory=lambda url: FakeProvider(base_url=url),
    )
    assert reg.binding("large").model == "qwen3:32b"
