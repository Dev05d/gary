"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api import routes_chat, routes_status, ws
from backend.chat.service import ChatService
from backend.config import REPO_ROOT, get_settings
from backend.database.session import create_all, dispose_engine, init_engine
from backend.events.bus import get_bus
from backend.llm.registry import LLMRegistry, set_registry
from backend.version import VERSION

log = logging.getLogger("gary")

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    )

    settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    init_engine(settings)
    await create_all()

    registry = LLMRegistry(settings)
    set_registry(registry)

    app.state.settings = settings
    app.state.registry = registry
    app.state.chat_service = ChatService(registry, settings)
    app.state.started_at = time.time()

    log.info("Gary v%s ready on http://%s:%s", VERSION, settings.app_host, settings.app_port)
    log.info("  chat model : %s (ctx %s)", settings.llm_model_large, settings.llm_context_large)
    log.info("  fast model : %s (ctx %s)", settings.llm_model_fast, settings.llm_context_fast)
    log.info("  ollama     : %s", settings.ollama_base_url)
    if settings.ollama_embed_base_url:
        log.info("  embeddings : %s", settings.embed_base_url)

    await get_bus().emit("system", "system.started", version=VERSION)

    try:
        yield
    finally:
        await registry.aclose()
        await dispose_engine()
        set_registry(None)
        log.info("Gary shut down cleanly")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Gary — Local Personal Intelligence Agent",
        version=VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(routes_status.router)
    app.include_router(routes_chat.router)
    app.include_router(ws.router)

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA if `npm run build` has been run.

    In development the Vite dev server handles this and proxies /api here, so
    a missing dist/ is normal and not an error.
    """
    if not FRONTEND_DIST.is_dir():
        @app.get("/", include_in_schema=False)
        async def _dev_hint() -> dict:
            return {
                "app": "gary",
                "version": VERSION,
                "ui": "Frontend not built. Run `npm run dev` in ./frontend "
                "(http://localhost:5173), or `npm run build` to serve it from here.",
                "api_docs": "/api/docs",
            }

        return

    app.mount(
        "/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets"
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
