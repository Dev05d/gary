"""Connecting accounts. OAuth tokens must never reach the browser."""

from __future__ import annotations

import base64
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from backend.database.models import OAuthCredential, Source, SyncState
from backend.security.crypto import Cipher, generate_key, reset_cipher


@pytest.fixture(autouse=True)
def _reset_cipher():
    reset_cipher()
    yield
    reset_cipher()


def configure_google(client) -> None:
    settings = client.app.state.settings  # type: ignore[attr-defined]
    settings.google_client_id = "test-client-id.apps.googleusercontent.com"
    settings.google_client_secret = "test-secret"
    settings.credential_encryption_key = generate_key()


# ===========================================================================
# Setup guidance
# ===========================================================================

async def test_unconfigured_google_explains_what_to_do(client):
    body = (await client.get("/api/sources")).json()
    assert body["google_configured"] is False
    assert "GOOGLE_CLIENT_ID" in body["setup_hint"]
    assert "redirect URI" in body["setup_hint"]


async def test_missing_encryption_key_is_called_out(client):
    settings = client.app.state.settings  # type: ignore[attr-defined]
    settings.google_client_id = "id"
    settings.google_client_secret = "secret"
    settings.credential_encryption_key = None

    body = (await client.get("/api/sources")).json()
    assert body["google_configured"] is False
    assert "CREDENTIAL_ENCRYPTION_KEY" in body["setup_hint"]


async def test_connect_refuses_without_configuration(client):
    r = await client.post("/api/auth/google/start")
    assert r.status_code == 400
    assert "GOOGLE_CLIENT_ID" in r.json()["detail"]["message"]


# ===========================================================================
# Authorisation URL
# ===========================================================================

async def test_authorization_url_requests_readonly_scopes_only(client):
    configure_google(client)
    r = await client.post("/api/auth/google/start")
    assert r.status_code == 200

    url = r.json()["authorization_url"]
    params = parse_qs(urlparse(url).query)
    scopes = params["scope"][0].split()

    assert "https://www.googleapis.com/auth/gmail.readonly" in scopes
    assert not any("gmail.send" in s or "gmail.modify" in s for s in scopes), (
        "a send or modify scope would break the read-only guarantee at the "
        "level Google enforces"
    )


async def test_authorization_url_forces_a_refresh_token(client):
    """Without prompt=consent Google omits the refresh token on re-auth."""
    configure_google(client)
    url = (await client.post("/api/auth/google/start")).json()["authorization_url"]
    params = parse_qs(urlparse(url).query)

    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]


async def test_authorization_url_uses_pkce(client):
    configure_google(client)
    url = (await client.post("/api/auth/google/start")).json()["authorization_url"]
    params = parse_qs(urlparse(url).query)

    assert params["code_challenge_method"] == ["S256"]
    assert len(params["code_challenge"][0]) > 20
    assert "code_verifier" not in params, "the verifier must never be sent"


async def test_each_attempt_gets_a_fresh_state(client):
    configure_google(client)
    first = (await client.post("/api/auth/google/start")).json()["authorization_url"]
    second = (await client.post("/api/auth/google/start")).json()["authorization_url"]
    state_a = parse_qs(urlparse(first).query)["state"][0]
    state_b = parse_qs(urlparse(second).query)["state"][0]
    assert state_a != state_b


# ===========================================================================
# Callback
# ===========================================================================

async def test_callback_rejects_an_unknown_state(client):
    """A forged callback must not be able to attach an account."""
    r = await client.get("/api/auth/google/callback?code=abc&state=not-a-real-state")
    assert r.status_code == 400
    assert "expired" in r.text.lower() or "unknown" in r.text.lower()


async def test_callback_reports_a_declined_consent(client):
    r = await client.get("/api/auth/google/callback?error=access_denied&state=x")
    assert r.status_code == 400
    assert "declined" in r.text.lower()


async def test_callback_is_not_authenticated(client):
    """Google cannot send a bearer token; `state` is the protection."""
    settings = client.app.state.settings  # type: ignore[attr-defined]
    settings.api_auth_token = "secret-token"
    try:
        r = await client.get("/api/auth/google/callback?error=access_denied&state=x")
        assert r.status_code != 401
    finally:
        settings.api_auth_token = None


# ===========================================================================
# Credential storage
# ===========================================================================

async def test_tokens_are_encrypted_at_rest_and_never_served(client, session):
    key = generate_key()
    cipher = Cipher.from_settings(key)

    source = Source(kind="gmail", account_identifier="me@example.com", status="connected")
    session.add(source)
    await session.flush()
    session.add(
        OAuthCredential(
            source_id=source.id,
            provider="google",
            account_email="me@example.com",
            encrypted_token=cipher.encrypt("super-secret-refresh", aad="me@example.com"),
        )
    )
    await session.commit()

    stored = (await session.execute(select(OAuthCredential))).scalar_one()
    assert b"super-secret-refresh" not in stored.encrypted_token
    assert stored.encrypted_token.startswith(b"g1:")

    body = (await client.get("/api/sources")).text
    assert "super-secret-refresh" not in body
    assert "encrypted_token" not in body


async def test_a_token_cannot_be_moved_between_accounts(client):
    """AAD binding: a blob copied to another row must fail to decrypt."""
    from backend.security.crypto import EncryptionError

    cipher = Cipher.from_settings(generate_key())
    blob = cipher.encrypt("token", aad="alice@example.com")

    assert cipher.decrypt(blob, aad="alice@example.com") == "token"
    with pytest.raises(EncryptionError):
        cipher.decrypt(blob, aad="bob@example.com")


async def test_tampered_ciphertext_is_rejected(client):
    from backend.security.crypto import EncryptionError

    cipher = Cipher.from_settings(generate_key())
    blob = bytearray(cipher.encrypt("token", aad="a@b.com"))
    blob[-1] ^= 0xFF

    with pytest.raises(EncryptionError, match="tampered|decrypt"):
        cipher.decrypt(bytes(blob), aad="a@b.com")


async def test_wrong_key_gives_an_actionable_error(client):
    from backend.security.crypto import EncryptionError

    blob = Cipher.from_settings(generate_key()).encrypt("token", aad="a@b.com")
    other = Cipher.from_settings(generate_key())

    with pytest.raises(EncryptionError, match="Reconnect"):
        other.decrypt(blob, aad="a@b.com")


# ===========================================================================
# Listing and lifecycle
# ===========================================================================

async def test_sources_list_reports_the_horizon(client, session):
    from backend.database.models import utcnow

    source = Source(
        kind="gmail",
        display_name="Gmail (me@example.com)",
        account_identifier="me@example.com",
        status="connected",
    )
    session.add(source)
    await session.flush()
    session.add(
        SyncState(
            source_id=source.id,
            watermark={"history_id": "12345"},
            recording_since=utcnow(),
            last_success_at=utcnow(),
            messages_ingested=42,
        )
    )
    await session.commit()

    body = (await client.get("/api/sources")).json()
    entry = next(s for s in body["sources"] if s["kind"] == "gmail")
    assert entry["status"] == "connected"
    assert entry["recording_since"] is not None
    assert entry["messages_ingested"] == 42


async def test_disconnecting_an_unknown_source_is_404(client):
    assert (await client.post("/api/sources/nope/disconnect")).status_code == 404


async def test_sync_now_on_an_unknown_source_is_404(client):
    assert (await client.post("/api/sources/nope/sync")).status_code == 404


async def test_disconnect_removes_the_credential(client, session):
    cipher = Cipher.from_settings(generate_key())
    source = Source(kind="gmail", account_identifier="me@example.com", status="connected")
    session.add(source)
    await session.flush()
    session.add(
        OAuthCredential(
            source_id=source.id,
            provider="google",
            account_email="me@example.com",
            encrypted_token=cipher.encrypt("t", aad="me@example.com"),
        )
    )
    await session.commit()

    settings = client.app.state.settings  # type: ignore[attr-defined]
    settings.credential_encryption_key = None  # revoke will fail; delete must not

    r = await client.post(f"/api/sources/{source.id}/disconnect")
    assert r.status_code == 200

    remaining = (await session.execute(select(OAuthCredential))).scalars().all()
    assert remaining == []

    await session.refresh(source)
    assert source.status == "disconnected"
    assert source.enabled is False
