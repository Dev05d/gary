"""Optional bearer-token auth for the local API.

Off by default: while bound to 127.0.0.1 there is no network attacker to
defend against, and forcing a token onto a localhost app is friction without
benefit. Setting API_AUTH_TOKEN turns it on for every /api route; `config.py`
refuses to boot on a non-loopback host unless it is set.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Header, HTTPException, status

from backend.config import Settings, get_settings


async def require_auth(
    authorization: Optional[str] = Header(default=None),
    x_gary_token: Optional[str] = Header(default=None, alias="X-Gary-Token"),
) -> None:
    settings: Settings = get_settings()
    expected = settings.api_auth_token
    if not expected:
        return

    presented = x_gary_token
    if not presented and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()

    # compare_digest to avoid leaking the token length/prefix via timing.
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
