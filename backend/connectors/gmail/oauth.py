"""Google OAuth 2.0 — authorisation code flow with PKCE.

Read-only scopes only. Gary has no send or modify capability, and the scopes
requested reflect that: even a total compromise of the agent cannot send mail,
because the token it holds does not carry the permission.

Tokens never reach the browser. The flow completes server-side, the refresh
token is encrypted with AES-256-GCM, and the frontend only ever learns that an
account is connected.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import urlencode

import httpx

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"

#: Read-only, deliberately. `gmail.readonly` cannot send, delete, or modify —
#: so the read-only guarantee is enforced by Google, not only by our tool list.
SCOPES: List[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

#: Refresh a little early — a token that expires mid-request produces a
#: confusing 401 rather than a clean refresh.
EXPIRY_SKEW = timedelta(minutes=5)


class OAuthError(RuntimeError):
    """The OAuth exchange failed."""


class ReauthRequired(OAuthError):
    """The refresh token is dead. Only re-consent fixes this.

    Distinct from a transient failure: retrying `invalid_grant` in a loop will
    never succeed, and the usual cause is worth telling the user about — a
    Google Cloud project left in "Testing" publishing status expires refresh
    tokens after 7 days.
    """


@dataclass
class PendingAuth:
    """One in-flight authorisation, held only in memory."""

    state: str
    code_verifier: str
    created_at: float = field(default_factory=time.monotonic)

    def is_expired(self, ttl_seconds: float = 600) -> bool:
        return (time.monotonic() - self.created_at) > ttl_seconds


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: Optional[str]
    expires_at: datetime
    scopes: List[str] = field(default_factory=list)
    account_email: str = ""

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= (self.expires_at - EXPIRY_SKEW)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_pending_auth() -> PendingAuth:
    """Fresh state and PKCE verifier.

    PKCE is not strictly required for a confidential client, but the redirect
    here is a loopback URL that any local process can listen on. The verifier
    means an intercepted authorisation code is useless on its own.
    """
    return PendingAuth(
        state=_b64url(secrets.token_bytes(32)),
        code_verifier=_b64url(secrets.token_bytes(64)),
    )


def authorization_url(
    *, client_id: str, redirect_uri: str, pending: PendingAuth, login_hint: str = ""
) -> str:
    challenge = _b64url(hashlib.sha256(pending.code_verifier.encode()).digest())
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": pending.state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # offline + consent is what actually yields a refresh token. Without
        # prompt=consent Google omits it on re-authorisation, and the account
        # silently stops working an hour later.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def _parse_token_response(payload: Dict, *, fallback_refresh: Optional[str] = None) -> TokenBundle:
    if "access_token" not in payload:
        raise OAuthError(f"Token response contained no access_token: {payload}")
    expires_in = int(payload.get("expires_in", 3600))
    return TokenBundle(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or fallback_refresh,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        scopes=(payload.get("scope") or "").split(),
    )


async def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: str,
    client: Optional[httpx.AsyncClient] = None,
) -> TokenBundle:
    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        if resp.status_code >= 400:
            raise OAuthError(_describe_failure(resp))
        bundle = _parse_token_response(resp.json())

        if not bundle.refresh_token:
            raise OAuthError(
                "Google returned no refresh token. Gary cannot keep syncing without "
                "one. This usually means the account was already authorised — revoke "
                "Gary's access at myaccount.google.com/permissions and try again."
            )

        bundle.account_email = await fetch_account_email(bundle.access_token, client=client)
        return bundle
    finally:
        if owns:
            await client.aclose()


async def refresh_access_token(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    client: Optional[httpx.AsyncClient] = None,
) -> TokenBundle:
    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code >= 400:
            payload = _safe_json(resp)
            if payload.get("error") in ("invalid_grant", "unauthorized_client"):
                raise ReauthRequired(
                    "Google rejected the stored refresh token. It was revoked, or it "
                    "expired — a Google Cloud project left in 'Testing' publishing "
                    "status expires refresh tokens after 7 days. Reconnect the "
                    "account, and set the project to 'In production' to stop this "
                    "recurring."
                )
            raise OAuthError(_describe_failure(resp))
        # A refresh response omits the refresh token; keep the one we have.
        return _parse_token_response(resp.json(), fallback_refresh=refresh_token)
    finally:
        if owns:
            await client.aclose()


async def fetch_account_email(
    access_token: str, *, client: Optional[httpx.AsyncClient] = None
) -> str:
    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.get(
            USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
        )
        if resp.status_code >= 400:
            return ""
        return resp.json().get("email", "")
    except httpx.HTTPError:
        return ""
    finally:
        if owns:
            await client.aclose()


async def revoke(
    token: str, *, client: Optional[httpx.AsyncClient] = None
) -> bool:
    """Tell Google to invalidate the token. Best effort."""
    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(REVOKE_ENDPOINT, data={"token": token})
        return resp.status_code < 400
    except httpx.HTTPError:
        return False
    finally:
        if owns:
            await client.aclose()


def _safe_json(resp: httpx.Response) -> Dict:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _describe_failure(resp: httpx.Response) -> str:
    payload = _safe_json(resp)
    error = payload.get("error", resp.status_code)
    detail = payload.get("error_description", resp.text[:200])

    if error == "redirect_uri_mismatch":
        return (
            "Google rejected the redirect URI. The value in your Google Cloud "
            "OAuth client must match GOOGLE_REDIRECT_URI exactly, including the "
            "port and trailing path."
        )
    if error == "invalid_client":
        return (
            "Google rejected the client credentials. Check GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET against the OAuth client in Google Cloud."
        )
    if error == "access_denied":
        return "Authorisation was declined at the Google consent screen."
    return f"Google returned {error}: {detail}"
