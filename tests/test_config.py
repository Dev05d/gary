from __future__ import annotations

import base64
import secrets

import pytest
from pydantic import ValidationError

from backend.config import Settings


def _s(**kw) -> Settings:
    kw.setdefault("_env_file", None)
    return Settings(**kw)


def test_model_roles_are_configurable_not_hardcoded():
    s = _s(
        llm_model_large="mymodel:70b",
        llm_model_fast="mymodel:8b",
        llm_context_large=65536,
    )
    assert s.model_for("large") == "mymodel:70b"
    assert s.model_for("fast") == "mymodel:8b"
    assert s.context_for("large") == 65536


def test_remote_ollama_url_is_used_for_all_roles():
    s = _s(ollama_base_url="http://192.168.1.42:11434")
    assert s.base_url_for("large") == "http://192.168.1.42:11434"
    assert s.base_url_for("embed") == "http://192.168.1.42:11434"


def test_embeddings_can_live_on_a_separate_host():
    s = _s(
        ollama_base_url="http://192.168.1.42:11434",
        ollama_embed_base_url="http://192.168.1.99:11434",
    )
    assert s.base_url_for("large") == "http://192.168.1.42:11434"
    assert s.base_url_for("embed") == "http://192.168.1.99:11434"


def test_trailing_slash_is_stripped_from_urls():
    s = _s(ollama_base_url="http://desktop.local:11434/")
    assert s.ollama_base_url == "http://desktop.local:11434"


def test_blank_embed_url_falls_back_to_main():
    s = _s(ollama_base_url="http://a:11434", ollama_embed_base_url="   ")
    assert s.embed_base_url == "http://a:11434"


def test_cors_origins_parsed_from_comma_separated_string():
    s = _s(CORS_ORIGINS="http://a.local, http://b.local ,")
    assert s.cors_origins == ["http://a.local", "http://b.local"]


def test_refuses_network_binding_without_a_token():
    with pytest.raises(ValidationError, match="API_AUTH_TOKEN"):
        _s(app_host="0.0.0.0", api_auth_token=None)


def test_allows_network_binding_with_a_token():
    s = _s(app_host="0.0.0.0", api_auth_token="hunter2")
    assert s.app_host == "0.0.0.0"


def test_loopback_without_token_is_fine():
    assert _s(app_host="127.0.0.1").api_auth_token is None


def test_encryption_key_must_be_32_bytes():
    good = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    assert len(_s(credential_encryption_key=good).encryption_key_bytes()) == 32

    bad = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode()
    with pytest.raises(ValueError, match="32 bytes"):
        _s(credential_encryption_key=bad).encryption_key_bytes()
