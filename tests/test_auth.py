"""Optional bearer-token auth."""

from __future__ import annotations

import time

import httpx
import pytest
import pytest_asyncio

from backend.chat.service import ChatService

TOKEN = "s3cret-token"


@pytest_asyncio.fixture
async def authed_client(db, settings, registry):
    import backend.api.routes_status as routes_status
    import backend.config as config_module
    import backend.security.auth as auth_module
    from backend.main import create_app

    settings.api_auth_token = TOKEN
    original = config_module.get_settings
    patched = lambda: settings  # noqa: E731
    config_module.get_settings = patched
    routes_status.get_settings = patched
    auth_module.get_settings = patched
    try:
        app = create_app()
        app.state.settings = settings
        app.state.registry = registry
        app.state.chat_service = ChatService(registry, settings)
        app.state.started_at = time.time()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        config_module.get_settings = original
        routes_status.get_settings = original
        auth_module.get_settings = original


async def test_health_stays_open_for_liveness_probes(authed_client):
    assert (await authed_client.get("/api/health")).status_code == 200


async def test_protected_route_rejects_missing_token(authed_client):
    assert (await authed_client.get("/api/status")).status_code == 401


async def test_protected_route_rejects_wrong_token(authed_client):
    r = await authed_client.get("/api/status", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_bearer_token_accepted(authed_client):
    r = await authed_client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


async def test_custom_header_accepted(authed_client):
    r = await authed_client.get("/api/status", headers={"X-Gary-Token": TOKEN})
    assert r.status_code == 200


async def test_chat_is_protected(authed_client):
    assert (await authed_client.post("/api/chat", json={"message": "hi"})).status_code == 401


async def test_no_token_configured_means_open_localhost_api(client):
    assert (await client.get("/api/status")).status_code == 200
