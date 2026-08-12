"""Connecting accounts, and reporting what they hold.

OAuth completes entirely server-side. The browser never sees a Google token —
it only learns that an account is connected.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.gmail import oauth
from backend.database.models import (
    Message,
    OAuthCredential,
    Source,
    SyncState,
    utcnow,
)
from backend.database.session import get_session
from backend.events.bus import get_bus
from backend.pipeline.horizon import SourceHorizon, staleness_warning
from backend.security.auth import require_auth
from backend.security.crypto import EncryptionError, get_cipher

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources", tags=["sources"])
auth_router = APIRouter(prefix="/api/auth/google", tags=["auth"])


class SourceOut(BaseModel):
    id: str
    kind: str
    display_name: str
    account: Optional[str] = None
    status: str
    enabled: bool
    recording_since: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    messages_ingested: int = 0
    messages_skipped: int = 0


class SourcesResponse(BaseModel):
    sources: List[SourceOut] = Field(default_factory=list)
    staleness_warning: Optional[str] = None
    google_configured: bool = False
    setup_hint: Optional[str] = None


def _settings(request: Request):
    from backend.config import get_settings

    return getattr(request.app.state, "settings", None) or get_settings()


@router.get("", response_model=SourcesResponse, dependencies=[Depends(require_auth)])
async def list_sources(
    request: Request, session: AsyncSession = Depends(get_session)
) -> SourcesResponse:
    settings = _settings(request)
    rows = (await session.execute(select(Source))).scalars().all()

    out: List[SourceOut] = []
    horizons: List[SourceHorizon] = []
    for src in rows:
        state = await session.get(SyncState, src.id)
        out.append(
            SourceOut(
                id=src.id,
                kind=src.kind,
                display_name=src.display_name or src.kind,
                account=src.account_identifier,
                status=src.status,
                enabled=src.enabled,
                recording_since=state.recording_since if state else None,
                last_success_at=state.last_success_at if state else None,
                last_error=(state.last_error if state else None) or src.last_error,
                consecutive_failures=state.consecutive_failures if state else 0,
                messages_ingested=state.messages_ingested if state else 0,
                messages_skipped=state.messages_skipped if state else 0,
            )
        )
        horizons.append(
            SourceHorizon(
                kind=src.kind,
                display_name=src.display_name or src.kind,
                recording_since=state.recording_since if state else None,
                last_sync_at=state.last_success_at if state else None,
                connected=src.status == "connected",
            )
        )

    configured = bool(settings.google_client_id and settings.google_client_secret)
    hint = None
    if not configured:
        hint = (
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env. Create them at "
            "console.cloud.google.com → APIs & Services → Credentials → OAuth client "
            "ID (Web application), and add "
            f"{settings.google_redirect_uri} as an authorised redirect URI."
        )
    elif not settings.credential_encryption_key:
        hint = (
            "CREDENTIAL_ENCRYPTION_KEY is not set, so OAuth tokens cannot be stored "
            "securely. Generate one with: python -c \"import secrets,base64;"
            "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\""
        )

    return SourcesResponse(
        sources=out,
        staleness_warning=staleness_warning(horizons),
        google_configured=configured and bool(settings.credential_encryption_key),
        setup_hint=hint,
    )


class ConnectResponse(BaseModel):
    authorization_url: str


@auth_router.post("/start", response_model=ConnectResponse, dependencies=[Depends(require_auth)])
async def start_google_auth(request: Request) -> ConnectResponse:
    settings = _settings(request)
    if not (settings.google_client_id and settings.google_client_secret):
        raise HTTPException(
            status_code=400,
            detail={
                "key": "google_client_id",
                "message": (
                    "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and "
                    "GOOGLE_CLIENT_SECRET in .env and restart."
                ),
            },
        )
    try:
        get_cipher(settings.credential_encryption_key)
    except EncryptionError as exc:
        raise HTTPException(
            status_code=400, detail={"key": "credential_encryption_key", "message": str(exc)}
        )

    pending = oauth.new_pending_auth()
    # Held in memory only, and short-lived: a state value that outlives the
    # process is a replay window for nobody's benefit.
    request.app.state.pending_auth[pending.state] = pending

    return ConnectResponse(
        authorization_url=oauth.authorization_url(
            client_id=settings.google_client_id,
            redirect_uri=settings.google_redirect_uri,
            pending=pending,
        )
    )


def _callback_page(title: str, body: str, ok: bool) -> HTMLResponse:
    colour = "#3fb950" if ok else "#f85149"
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<title>{title}</title>
<body style="font-family:ui-sans-serif,system-ui;background:#0b0d10;color:#e6e9ef;
             display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
  <div style="max-width:520px;padding:32px;border:1px solid #232830;border-radius:14px;
              background:#111419">
    <h1 style="font-size:18px;margin:0 0 10px;color:{colour}">{title}</h1>
    <p style="color:#8b93a1;font-size:14px;line-height:1.6;margin:0">{body}</p>
    <p style="color:#5c6472;font-size:12.5px;margin-top:18px">You can close this tab.</p>
  </div>
  <script>setTimeout(() => window.close(), 4000)</script>
</body>""",
        status_code=200 if ok else 400,
    )


@auth_router.get("/callback")
async def google_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
) -> HTMLResponse:
    """Google redirects here. Deliberately unauthenticated — Google cannot send
    a bearer token — but protected by the one-time `state` value."""
    settings = _settings(request)

    if error:
        return _callback_page("Authorisation declined", f"Google reported: {error}", False)

    pending = request.app.state.pending_auth.pop(state, None)
    if pending is None or pending.is_expired():
        return _callback_page(
            "That link has expired",
            "The authorisation request is unknown or older than ten minutes. "
            "Start again from Gary's Sources page.",
            False,
        )
    if not code:
        return _callback_page("No authorisation code", "Google returned no code.", False)

    try:
        bundle = await oauth.exchange_code(
            code=code,
            client_id=settings.google_client_id or "",
            client_secret=settings.google_client_secret or "",
            redirect_uri=settings.google_redirect_uri,
            code_verifier=pending.code_verifier,
        )
    except oauth.OAuthError as exc:
        return _callback_page("Could not connect", str(exc), False)

    account = bundle.account_email or "unknown"

    try:
        cipher = get_cipher(settings.credential_encryption_key)
        blob = cipher.encrypt(bundle.refresh_token or "", aad=account)
    except EncryptionError as exc:
        return _callback_page("Could not store the credential", str(exc), False)

    for kind, label in (("gmail", "Gmail"), ("gcal", "Google Calendar")):
        source = (
            await session.execute(
                select(Source).where(
                    Source.kind == kind, Source.account_identifier == account
                )
            )
        ).scalar_one_or_none()
        if source is None:
            source = Source(kind=kind, account_identifier=account)
            session.add(source)
            await session.flush()
        source.display_name = f"{label} ({account})"
        source.status = "connected"
        source.enabled = True
        source.last_error = None

        if kind == "gmail":
            credential = (
                await session.execute(
                    select(OAuthCredential).where(
                        OAuthCredential.provider == "google",
                        OAuthCredential.account_email == account,
                    )
                )
            ).scalar_one_or_none()
            if credential is None:
                credential = OAuthCredential(
                    source_id=source.id, provider="google", account_email=account
                )
                session.add(credential)
            credential.source_id = source.id
            credential.encrypted_token = blob
            credential.scopes = bundle.scopes
            credential.expires_at = bundle.expires_at
            credential.updated_at = utcnow()

    await session.commit()
    await get_bus().emit("system", "source.connected", account=account, kinds=["gmail", "gcal"])

    return _callback_page(
        "Connected",
        f"Gary is now watching <strong>{account}</strong>. Only mail that arrives "
        "from this moment on is stored — nothing historical is imported.",
        True,
    )


@router.post("/{source_id}/disconnect", dependencies=[Depends(require_auth)])
async def disconnect(
    source_id: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Dict[str, Any]:
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    settings = _settings(request)
    credential = (
        await session.execute(
            select(OAuthCredential).where(OAuthCredential.source_id == source_id)
        )
    ).scalar_one_or_none()

    if credential is not None:
        # Best effort: tell Google to invalidate it, then drop our copy either way.
        try:
            token = get_cipher(settings.credential_encryption_key).decrypt(
                credential.encrypted_token, aad=credential.account_email
            )
            await oauth.revoke(token)
        except (EncryptionError, Exception):  # noqa: BLE001
            log.warning("could not revoke token for %s", source_id, exc_info=True)
        await session.delete(credential)

    source.status = "disconnected"
    source.enabled = False
    await session.commit()
    return {"disconnected": True, "source_id": source_id}


class SyncNowResponse(BaseModel):
    ran: bool
    summary: str = ""
    error: Optional[str] = None


@router.post("/{source_id}/sync", response_model=SyncNowResponse, dependencies=[Depends(require_auth)])
async def sync_now(
    source_id: str, request: Request, session: AsyncSession = Depends(get_session)
) -> SyncNowResponse:
    """Run one sync immediately, rather than waiting for the poll interval."""
    from backend.workers.gmail_worker import sync_source

    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    outcome, error = await sync_source(
        session, source=source, settings=_settings(request)
    )
    await session.commit()
    if error:
        return SyncNowResponse(ran=False, error=error)
    return SyncNowResponse(ran=True, summary=outcome.summary() if outcome else "")
