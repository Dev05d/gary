from __future__ import annotations

import os
from typing import AsyncIterator, List

import pytest
import pytest_asyncio

# Point every test at an isolated in-memory-ish DB and a fake LLM *before*
# backend.config is imported anywhere.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/test_gary.db")
os.environ.setdefault("API_AUTH_TOKEN", "")
os.environ.setdefault("APP_HOST", "127.0.0.1")

from backend.chat.service import ChatService  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.database.session import (  # noqa: E402
    create_all,
    dispose_engine,
    init_engine,
)
from backend.events.bus import reset_bus  # noqa: E402
from backend.llm.registry import LLMRegistry  # noqa: E402
from backend.settings.service import SettingsService  # noqa: E402
from tests.fakes import FakeProvider  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'gary.db'}",
        data_dir=tmp_path,
        ollama_base_url="http://fake-ollama:11434",
        llm_model_large="fake-large",
        llm_model_fast="fake-fast",
        llm_model_router="fake-fast",
        embedding_model="fake-embed",
        llm_context_large=4096,
        llm_context_fast=2048,
        llm_generation_buffer=256,
        api_auth_token=None,
    )


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def registry(settings: Settings, fake_provider: FakeProvider) -> LLMRegistry:
    return LLMRegistry(settings, provider_factory=lambda _url: fake_provider)


@pytest_asyncio.fixture
async def db(settings: Settings) -> AsyncIterator[None]:
    init_engine(settings)
    await create_all()
    try:
        yield
    finally:
        await dispose_engine()


@pytest_asyncio.fixture
async def session(db) -> AsyncIterator:
    from backend.database.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        yield s


@pytest_asyncio.fixture
async def client(
    db, settings: Settings, registry: LLMRegistry, fake_provider: FakeProvider
) -> AsyncIterator:
    """HTTP client wired to the real app with a fake LLM behind it.

    httpx.ASGITransport does not run lifespan, so the state that `lifespan()`
    would populate is set here instead. The DB engine comes from the `db`
    fixture, and `get_settings` is patched so request-scoped lookups (auth,
    status) see the test settings rather than the developer's own .env.
    """
    import time

    import httpx

    import backend.api.routes_status as routes_status
    import backend.api.ws as ws_module
    import backend.config as config_module
    import backend.security.auth as auth_module
    from backend.main import create_app

    original = config_module.get_settings
    patched = lambda: settings  # noqa: E731
    config_module.get_settings = patched  # type: ignore[assignment]
    routes_status.get_settings = patched  # type: ignore[assignment]
    auth_module.get_settings = patched  # type: ignore[assignment]
    ws_module.get_settings = patched  # type: ignore[assignment]
    try:
        app = create_app()
        app.state.settings = settings
        app.state.settings_service = SettingsService(settings)
        app.state.registry = registry
        app.state.chat_service = ChatService(registry, settings)
        app.state.started_at = time.time()
        app.state.pending_restart = set()
        # Survive settings reloads without reaching for a real Ollama. The
        # single shared instance keeps `.calls` inspectable; reflecting the
        # requested URL back lets tests assert that a rebuild actually
        # re-pointed the registry.
        def _factory(url: str) -> FakeProvider:
            fake_provider.base_url = url
            return fake_provider

        app.state.provider_factory = _factory

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c
    finally:
        config_module.get_settings = original  # type: ignore[assignment]
        routes_status.get_settings = original  # type: ignore[assignment]
        auth_module.get_settings = original  # type: ignore[assignment]
        ws_module.get_settings = original  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _clean_bus() -> AsyncIterator[None]:
    reset_bus()
    yield
    reset_bus()


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "asyncio: async test")
