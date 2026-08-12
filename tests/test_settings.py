"""Settings overlay: persistence, provenance, validation, and hot reload."""

from __future__ import annotations

import pytest

from backend.settings.catalog import BY_KEY, CATALOG, SettingValidationError, coerce, validate


# --------------------------------------------------------------- catalog

def test_every_catalog_key_exists_on_settings():
    """A typo here would silently create a setting that never applies."""
    from backend.config import Settings

    missing = [d.key for d in CATALOG if d.key not in Settings.model_fields]
    assert missing == [], f"catalog keys with no Settings field: {missing}"


def test_catalog_keys_are_unique():
    keys = [d.key for d in CATALOG]
    assert len(keys) == len(set(keys))


def test_every_field_has_a_real_description():
    thin = [d.key for d in CATALOG if len(d.description) < 40]
    assert thin == [], f"these need fuller explanations: {thin}"


def test_inactive_fields_declare_a_future_milestone():
    for d in CATALOG:
        if not d.active:
            assert d.milestone > 1, f"{d.key} is inactive but claims milestone 1"


def test_secrets_are_marked_sensitive():
    assert BY_KEY["api_auth_token"].sensitive is True


def test_coerce_blank_string_means_unset():
    assert coerce(BY_KEY["llm_temperature_large"], "") is None
    assert coerce(BY_KEY["ollama_embed_base_url"], "   ") is None


def test_coerce_types():
    assert coerce(BY_KEY["llm_context_large"], "16384") == 16384
    assert coerce(BY_KEY["llm_temperature"], "0.7") == 0.7
    assert coerce(BY_KEY["ui_stream_responses"], "false") is False
    assert coerce(BY_KEY["ui_stream_responses"], True) is True


def test_validate_rejects_out_of_range():
    with pytest.raises(SettingValidationError):
        validate(BY_KEY["llm_temperature"], 5.0)
    with pytest.raises(SettingValidationError):
        validate(BY_KEY["llm_context_large"], 10)


def test_validate_rejects_bad_url_and_option():
    with pytest.raises(SettingValidationError, match="http"):
        validate(BY_KEY["ollama_base_url"], "192.168.1.42:11434")
    with pytest.raises(SettingValidationError):
        validate(BY_KEY["log_level"], "CHATTY")


# ------------------------------------------------------------------ API

async def test_read_returns_catalog_with_values_and_provenance(client):
    body = (await client.get("/api/settings")).json()
    assert len(body["fields"]) == len(CATALOG)
    assert len(body["categories"]) >= 8

    field = next(f for f in body["fields"] if f["key"] == "llm_model_large")
    assert field["value"] == "fake-large"
    assert field["source"] in ("default", "env", "database")
    assert len(field["description"]) > 40


async def test_secret_values_are_never_sent_to_the_browser(client):
    await client.patch("/api/settings", json={"changes": {"api_auth_token": "s3cret"}})
    # Setting a token immediately makes auth mandatory, so re-read with it.
    auth = {"Authorization": "Bearer s3cret"}
    resp = await client.get("/api/settings", headers=auth)
    assert resp.status_code == 200

    token_field = next(f for f in resp.json()["fields"] if f["key"] == "api_auth_token")
    assert token_field["value"] is None
    assert token_field["is_set"] is True
    assert token_field["env_value"] is None
    assert "s3cret" not in resp.text, "the token must never appear in the payload"


async def test_update_persists_and_reports_provenance(client):
    r = await client.patch(
        "/api/settings", json={"changes": {"llm_context_large": 16384}}
    )
    assert r.status_code == 200
    assert r.json()["changed"] == ["llm_context_large"]

    body = (await client.get("/api/settings")).json()
    field = next(f for f in body["fields"] if f["key"] == "llm_context_large")
    assert field["value"] == 16384
    assert field["source"] == "database"
    assert field["overridden"] is True


async def test_changing_the_model_applies_without_a_restart(client, fake_provider):
    """The whole point of the overlay: no restart to switch models."""
    await client.patch("/api/settings", json={"changes": {"llm_model_large": "swapped:70b"}})

    await client.post("/api/chat", json={"message": "hi"})
    assert fake_provider.calls[-1]["model"] == "swapped:70b"


async def test_changing_context_window_applies_immediately(client, fake_provider):
    await client.patch("/api/settings", json={"changes": {"llm_context_large": 16384}})
    await client.post("/api/chat", json={"message": "hi"})
    assert fake_provider.calls[-1]["num_ctx"] == 16384


async def test_changing_ollama_url_rebuilds_the_registry(client):
    await client.patch(
        "/api/settings", json={"changes": {"ollama_base_url": "http://192.168.1.99:11434"}}
    )
    status = (await client.get("/api/status")).json()
    assert status["models"][0]["base_url"] == "http://192.168.1.99:11434"


async def test_per_role_temperature_override(client, fake_provider):
    await client.patch(
        "/api/settings",
        json={"changes": {"llm_temperature": 0.9, "llm_temperature_fast": 0.0}},
    )
    from backend.config import Settings

    settings = client.app.state.settings  # type: ignore[attr-defined]
    assert isinstance(settings, Settings)
    assert settings.temperature_for("large") == 0.9  # falls back to global
    assert settings.temperature_for("fast") == 0.0  # explicit override
    assert settings.temperature_for("router") == 0.0  # always deterministic


async def test_invalid_value_is_rejected_with_a_useful_message(client):
    r = await client.patch("/api/settings", json={"changes": {"llm_temperature": 99}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["key"] == "llm_temperature"
    assert "at most" in detail["message"]


async def test_unknown_setting_is_rejected(client):
    r = await client.patch("/api/settings", json={"changes": {"not_a_setting": 1}})
    assert r.status_code == 422


async def test_a_bad_value_in_a_batch_rolls_back_the_whole_batch(client):
    r = await client.patch(
        "/api/settings",
        json={"changes": {"llm_context_large": 16384, "llm_temperature": 99}},
    )
    assert r.status_code == 422

    body = (await client.get("/api/settings")).json()
    field = next(f for f in body["fields"] if f["key"] == "llm_context_large")
    assert field["value"] == 4096, "the valid change should not have been applied"


async def test_cannot_expose_to_the_network_without_a_token(client):
    """The config-level guard must survive being driven from the UI."""
    r = await client.patch("/api/settings", json={"changes": {"app_host": "0.0.0.0"}})
    assert r.status_code == 422
    assert "API_AUTH_TOKEN" in r.json()["detail"]["message"]


async def test_network_binding_allowed_once_a_token_is_set(client):
    await client.patch("/api/settings", json={"changes": {"api_auth_token": "tok"}})
    r = await client.patch(
        "/api/settings",
        json={"changes": {"app_host": "0.0.0.0"}},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    assert "app_host" in r.json()["restart_required"]


async def test_restart_only_settings_are_flagged(client):
    r = await client.patch("/api/settings", json={"changes": {"app_port": 9000}})
    assert r.json()["restart_required"] == ["app_port"]
    assert r.json()["applied_live"] is False


async def test_live_settings_are_not_flagged_for_restart(client):
    r = await client.patch("/api/settings", json={"changes": {"llm_context_fast": 4096}})
    assert r.json()["restart_required"] == []
    assert r.json()["applied_live"] is True


async def test_reset_one_falls_back_to_env(client):
    await client.patch("/api/settings", json={"changes": {"llm_model_large": "temp:1b"}})
    r = await client.post("/api/settings/reset/llm_model_large")
    assert r.status_code == 200

    field = next(f for f in r.json()["fields"] if f["key"] == "llm_model_large")
    assert field["value"] == "fake-large"
    assert field["overridden"] is False


async def test_reset_all_clears_every_override(client):
    await client.patch(
        "/api/settings",
        json={"changes": {"llm_model_large": "x:1b", "llm_context_fast": 4096}},
    )
    r = await client.post("/api/settings/reset")
    assert all(not f["overridden"] for f in r.json()["fields"])


async def test_empty_string_clears_an_override(client):
    await client.patch("/api/settings", json={"changes": {"llm_temperature_fast": 0.1}})
    await client.patch("/api/settings", json={"changes": {"llm_temperature_fast": ""}})
    body = (await client.get("/api/settings")).json()
    field = next(f for f in body["fields"] if f["key"] == "llm_temperature_fast")
    assert field["overridden"] is False


async def test_ui_prefs_are_exposed_on_status(client):
    await client.patch(
        "/api/settings",
        json={"changes": {"ui_default_role": "fast", "ui_show_context_meter": False}},
    )
    ui = (await client.get("/api/status")).json()["ui"]
    assert ui["default_role"] == "fast"
    assert ui["show_context_meter"] is False


async def test_max_history_turns_is_honoured(client, fake_provider):
    await client.patch("/api/settings", json={"changes": {"max_history_turns": 2}})

    r = await client.post("/api/chat", json={"message": "m1"})
    import json as _json

    convo = _json.loads(r.text.split("data: ")[1].split("\n")[0])["conversation_id"]
    for i in range(2, 6):
        await client.post(
            "/api/chat", json={"message": f"m{i}", "conversation_id": convo}
        )

    # system + at most 2 history messages + the current question
    assert len(fake_provider.calls[-1]["messages"]) <= 4


async def test_test_connection_probes_without_saving(client):
    r = await client.post(
        "/api/settings/test-connection", json={"base_url": "http://192.168.1.50:11434"}
    )
    assert r.status_code == 200
    assert r.json()["base_url"] == "http://192.168.1.50:11434"

    body = (await client.get("/api/settings")).json()
    field = next(f for f in body["fields"] if f["key"] == "ollama_base_url")
    assert field["value"] == "http://fake-ollama:11434", "probing must not persist"


async def test_test_connection_rejects_a_malformed_url(client):
    r = await client.post("/api/settings/test-connection", json={"base_url": "192.168.1.50"})
    assert r.status_code == 422


async def test_settings_require_auth_when_a_token_is_configured(client):
    await client.patch("/api/settings", json={"changes": {"api_auth_token": "abc"}})
    # The new token applies immediately — the next unauthenticated call fails.
    assert (await client.get("/api/settings")).status_code == 401
    assert (
        await client.get("/api/settings", headers={"Authorization": "Bearer abc"})
    ).status_code == 200
