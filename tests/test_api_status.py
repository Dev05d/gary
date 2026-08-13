from __future__ import annotations

from backend.llm.base import LLMUnavailableError


async def test_status_reports_connected_backend(client):
    body = (await client.get("/api/status")).json()
    assert body["milestone"] == 2
    assert body["database_connected"] is True
    assert body["read_only"] is True
    backend = body["llm_backends"][0]
    assert backend["connected"] is True
    assert backend["version"] == "fake-0.0.0"


async def test_status_flags_a_configured_model_that_is_not_installed(client, fake_provider):
    fake_provider.installed = ["fake-fast"]  # "fake-large" missing
    body = (await client.get("/api/status")).json()
    large = next(m for m in body["models"] if m["role"] == "large")
    assert large["available"] is False
    assert "ollama pull fake-large" in large["note"]


async def test_status_shows_the_configured_base_url_per_role(client):
    body = (await client.get("/api/status")).json()
    for model in body["models"]:
        assert model["base_url"] == "http://fake-ollama:11434"
        assert model["num_ctx"] > 0


async def test_status_survives_an_unreachable_backend(client, fake_provider):
    fake_provider.fail_with = LLMUnavailableError("connection refused")
    r = await client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["llm_backends"][0]["connected"] is False


async def test_status_counts_conversations(client):
    await client.post("/api/conversations", json={"title": "one"})
    body = (await client.get("/api/status")).json()
    assert body["counts"]["conversations"] == 1


async def test_connector_status_is_reported_honestly(client):
    """Built-but-unconnected and not-built-yet are different states."""
    body = (await client.get("/api/status")).json()
    kinds = {s["kind"]: s for s in body["sources"]}

    for kind in ("gmail", "gcal", "imessage"):
        assert kinds[kind]["implemented"] is True
        assert kinds[kind]["status"] == "disconnected", f"{kind}: built, but nothing connected"

    assert kinds["discord_export"]["implemented"] is False
    assert kinds["discord_export"]["status"] == "not_implemented"


async def test_status_reports_the_facts_plane(client):
    body = (await client.get("/api/status")).json()
    counts = body["counts"]
    assert counts["messages_indexed"] == 0
    assert counts["threads_indexed"] == 0
    assert counts["identities"] == 0
    assert body["horizons"] == [], "no sources connected yet"
