"""Settings API.

Reads and writes the database override layer, hot-reloading whatever can be
applied without a restart. Also provides the two probes the settings UI needs
to be useful rather than a wall of text boxes: "can you reach this host?" and
"what models are installed on it?".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.chat.service import ChatService
from backend.database.session import get_session
from backend.events.bus import get_bus
from backend.llm.base import LLMUnavailableError
from backend.llm.ollama_provider import OllamaProvider
from backend.llm.registry import LLMRegistry, set_registry
from backend.settings.catalog import CATEGORIES, SettingValidationError
from backend.settings.service import SettingsService
from backend.security.auth import require_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_auth)])


class CategoryOut(BaseModel):
    key: str
    label: str
    description: str


class SettingsResponse(BaseModel):
    categories: List[CategoryOut]
    fields: List[Dict[str, Any]]
    restart_required: List[str] = Field(default_factory=list)


class UpdateRequest(BaseModel):
    changes: Dict[str, Any]


class UpdateResponse(BaseModel):
    changed: List[str]
    restart_required: List[str]
    applied_live: bool
    fields: List[Dict[str, Any]]


class TestConnectionRequest(BaseModel):
    base_url: str


class TestConnectionResponse(BaseModel):
    connected: bool
    base_url: str
    version: Optional[str] = None
    latency_ms: Optional[float] = None
    models: List[str] = Field(default_factory=list)
    error: Optional[str] = None


def _service(request: Request) -> SettingsService:
    return request.app.state.settings_service


async def _reload_runtime(request: Request, new_settings) -> None:
    """Swap in settings that can take effect without a restart.

    The old registry is closed after the new one is in place so an in-flight
    request never loses its connection pool mid-stream.
    """
    old_registry: LLMRegistry = request.app.state.registry

    # app.state.provider_factory lets tests keep a fake provider across a
    # reload; in production it is unset and the real Ollama factory is used.
    factory = getattr(request.app.state, "provider_factory", None)
    registry = LLMRegistry(new_settings, provider_factory=factory)
    request.app.state.settings = new_settings
    request.app.state.registry = registry
    request.app.state.chat_service = ChatService(registry, new_settings)
    set_registry(registry)

    logging.getLogger().setLevel(
        getattr(logging, str(new_settings.log_level).upper(), logging.INFO)
    )

    try:
        await old_registry.aclose()
    except Exception:  # noqa: BLE001 - never fail a settings save on cleanup
        log.warning("closing the previous LLM registry failed", exc_info=True)


@router.get("", response_model=SettingsResponse)
async def read_settings(
    request: Request, session: AsyncSession = Depends(get_session)
) -> SettingsResponse:
    service = _service(request)
    fields = await service.describe(session, request.app.state.settings)
    return SettingsResponse(
        categories=[
            CategoryOut(key=c.key, label=c.label, description=c.description)
            for c in CATEGORIES
        ],
        fields=fields,
        restart_required=list(request.app.state.pending_restart),
    )


@router.patch("", response_model=UpdateResponse)
async def update_settings(
    body: UpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> UpdateResponse:
    service = _service(request)
    try:
        new_settings, changed, restart_needed = await service.apply(session, body.changes)
    except SettingValidationError as exc:
        raise HTTPException(status_code=422, detail={"key": exc.key, "message": exc.message})

    await _reload_runtime(request, new_settings)

    if restart_needed:
        request.app.state.pending_restart.update(restart_needed)

    if changed:
        await get_bus().emit("system", "settings.updated", keys=changed)
        log.info("settings updated: %s", ", ".join(changed))

    fields = await service.describe(session, new_settings)
    return UpdateResponse(
        changed=changed,
        restart_required=sorted(request.app.state.pending_restart),
        applied_live=bool(changed) and not restart_needed,
        fields=fields,
    )


@router.post("/reset/{key}", response_model=UpdateResponse)
async def reset_setting(
    key: str, request: Request, session: AsyncSession = Depends(get_session)
) -> UpdateResponse:
    service = _service(request)
    try:
        new_settings = await service.reset(session, key)
    except SettingValidationError as exc:
        raise HTTPException(status_code=404, detail={"key": exc.key, "message": exc.message})

    await _reload_runtime(request, new_settings)
    fields = await service.describe(session, new_settings)
    return UpdateResponse(
        changed=[key],
        restart_required=sorted(request.app.state.pending_restart),
        applied_live=True,
        fields=fields,
    )


@router.post("/reset", response_model=UpdateResponse)
async def reset_all_settings(
    request: Request, session: AsyncSession = Depends(get_session)
) -> UpdateResponse:
    service = _service(request)
    new_settings = await service.reset_all(session)
    await _reload_runtime(request, new_settings)
    fields = await service.describe(session, new_settings)
    return UpdateResponse(
        changed=["*"],
        restart_required=sorted(request.app.state.pending_restart),
        applied_live=True,
        fields=fields,
    )


@router.post("/test-connection", response_model=TestConnectionResponse)
async def test_connection(
    body: TestConnectionRequest, request: Request
) -> TestConnectionResponse:
    """Probe an Ollama host without saving anything.

    Lets you verify a LAN address before committing to it — the alternative is
    saving a broken URL and discovering it on your next message.
    """
    url = body.base_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=422,
            detail={"key": "base_url", "message": "URL must start with http:// or https://"},
        )

    settings = request.app.state.settings
    provider = OllamaProvider(url, timeout=min(float(settings.ollama_timeout), 20.0))
    try:
        health = await provider.health()
        return TestConnectionResponse(
            connected=health.connected,
            base_url=url,
            version=health.version,
            latency_ms=health.latency_ms,
            models=health.models,
            error=health.error,
        )
    finally:
        await provider.aclose()


@router.get("/models", response_model=List[str])
async def list_models(request: Request, base_url: Optional[str] = None) -> List[str]:
    """Model tags installed on a host, to populate the model pickers."""
    settings = request.app.state.settings
    url = (base_url or settings.ollama_base_url).strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return []

    provider = OllamaProvider(url, timeout=15.0)
    try:
        return sorted(await provider.list_models())
    except LLMUnavailableError:
        return []
    finally:
        await provider.aclose()
