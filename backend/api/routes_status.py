"""Health and observability endpoints (spec §19)."""

from __future__ import annotations

import asyncio
import time
from typing import List

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.chat.service import ChatService
from backend.config import get_settings
from backend.database.session import get_session, healthcheck
from backend.database.models import Identity, Message, MessageThread, Source, SyncState
from backend.events.bus import get_bus
from backend.pipeline.horizon import SourceHorizon, staleness_warning
from backend.schemas import (
    BackendStatus,
    CountsOut,
    HealthResponse,
    ModelInfo,
    HorizonOut,
    SourceStatus,
    StatusResponse,
    UiPrefs,
)
from backend.security.auth import require_auth
from backend.version import MILESTONE, VERSION

router = APIRouter(prefix="/api", tags=["status"])

#: Connectors on the roadmap. `implemented` flips as milestones land, so the
#: status page always tells the truth about what is actually wired up.
PLANNED_SOURCES = [
    ("gmail", "Gmail", True),
    ("gcal", "Google Calendar", False),
    ("imessage", "iMessage (local)", False),
    ("discord_export", "Discord (data export)", False),
    ("instagram_export", "Instagram (data export)", False),
    ("files", "Local files", False),
]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness only — deliberately unauthenticated and dependency-free."""
    return HealthResponse(status="ok", version=VERSION)


@router.get("/status", response_model=StatusResponse, dependencies=[Depends(require_auth)])
async def status(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StatusResponse:
    # app.state.settings, not get_settings(): the settings UI can change these
    # at runtime and the status page must reflect what is actually in use.
    settings = getattr(request.app.state, "settings", None) or get_settings()
    registry = request.app.state.registry
    service: ChatService = request.app.state.chat_service

    providers = registry.distinct_base_urls
    health_results = await asyncio.gather(
        *(p.health() for p in providers.values()), return_exceptions=True
    )

    backends: List[BackendStatus] = []
    installed: set[str] = set()
    for provider, result in zip(providers.values(), health_results):
        if isinstance(result, BaseException):
            backends.append(
                BackendStatus(
                    name=provider.name,
                    base_url=getattr(provider, "base_url", "?"),
                    connected=False,
                    error=str(result),
                )
            )
            continue
        installed.update(result.models)
        backends.append(
            BackendStatus(
                name=provider.name,
                base_url=result.base_url,
                connected=result.connected,
                version=result.version,
                latency_ms=result.latency_ms,
                error=result.error,
                installed_models=result.models,
            )
        )

    models: List[ModelInfo] = []
    for role in ("large", "fast", "router", "embed"):
        binding = registry.binding(role)  # type: ignore[arg-type]
        present = binding.model in installed
        models.append(
            ModelInfo(
                role=role,
                model=binding.model,
                num_ctx=binding.num_ctx,
                base_url=binding.base_url,
                available=present,
                note=(
                    None
                    if present
                    else f"Not installed. Run: ollama pull {binding.model}"
                ),
            )
        )

    convos, turns = await service.counts(session)

    # Real counts from the facts plane, not placeholders.
    messages_indexed = (
        await session.scalar(
            select(func.count()).select_from(Message).where(Message.deleted_at.is_(None))
        )
        or 0
    )
    threads_indexed = (
        await session.scalar(select(func.count()).select_from(MessageThread)) or 0
    )
    identity_count = await session.scalar(select(func.count()).select_from(Identity)) or 0

    configured = (await session.execute(select(Source))).scalars().all()
    horizons: List[HorizonOut] = []
    horizon_models: List[SourceHorizon] = []
    for src in configured:
        state = await session.get(SyncState, src.id)
        horizons.append(
            HorizonOut(
                kind=src.kind,
                display_name=src.display_name or src.kind,
                connected=src.status == "connected",
                recording_since=state.recording_since if state else None,
                last_sync_at=state.last_success_at if state else None,
            )
        )
        horizon_models.append(
            SourceHorizon(
                kind=src.kind,
                display_name=src.display_name or src.kind,
                recording_since=state.recording_since if state else None,
                last_sync_at=state.last_success_at if state else None,
                connected=src.status == "connected",
                covers_future=src.kind == "gcal",
            )
        )
    connected_kinds = {s.kind for s in configured if s.status == "connected"}

    return StatusResponse(
        version=VERSION,
        milestone=MILESTONE,
        database_connected=await healthcheck(),
        database_path=str(settings.sqlite_path() or settings.database_url),
        llm_backends=backends,
        models=models,
        sources=[
            SourceStatus(
                kind=kind,
                display_name=name,
                status=("connected" if kind in connected_kinds else
                        "disconnected" if done else "not_implemented"),
                enabled=kind in connected_kinds,
                implemented=done,
            )
            for kind, name, done in PLANNED_SOURCES
        ],
        counts=CountsOut(
            conversations=convos,
            chat_turns=turns,
            messages_indexed=messages_indexed,
            threads_indexed=threads_indexed,
            identities=identity_count,
        ),
        horizons=horizons,
        staleness_warning=staleness_warning(horizon_models),
        event_subscribers=get_bus().subscriber_count,
        read_only=True,
        uptime_seconds=round(time.time() - request.app.state.started_at, 1),
        ui=UiPrefs(
            default_role=settings.ui_default_role,
            show_context_meter=settings.ui_show_context_meter,
            show_tool_calls=settings.ui_show_tool_calls,
            stream_responses=settings.ui_stream_responses,
        ),
        restart_required=sorted(getattr(request.app.state, "pending_restart", set())),
    )
