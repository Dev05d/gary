"""Chat + conversation endpoints.

Streaming uses SSE rather than a WebSocket: it is a plain POST, survives
proxies, and reconnects are the browser's problem. The WebSocket at /ws/events
carries *system* events (ingestion progress, notifications), which is a
genuinely bidirectional-ish, long-lived concern.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.chat.service import ChatService
from backend.database.session import get_session
from backend.schemas import (
    ChatRequest,
    ConversationDetail,
    ConversationOut,
    CreateConversationIn,
)
from backend.security.auth import require_auth

router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_auth)])


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service


@router.get("/conversations", response_model=List[ConversationOut])
async def list_conversations(
    include_archived: bool = False,
    session: AsyncSession = Depends(get_session),
    service: ChatService = Depends(get_chat_service),
) -> List[ConversationOut]:
    rows = await service.list_conversations(session, include_archived=include_archived)
    return [ConversationOut.model_validate(r) for r in rows]


@router.post(
    "/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED
)
async def create_conversation(
    body: CreateConversationIn,
    session: AsyncSession = Depends(get_session),
    service: ChatService = Depends(get_chat_service),
) -> ConversationOut:
    convo = await service.create_conversation(session, body.title)
    return ConversationOut.model_validate(convo)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    service: ChatService = Depends(get_chat_service),
) -> ConversationDetail:
    convo = await service.get_conversation(session, conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetail.model_validate(convo)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    service: ChatService = Depends(get_chat_service),
) -> None:
    if not await service.delete_conversation(session, conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/chat")
async def chat(
    body: ChatRequest,
    session: AsyncSession = Depends(get_session),
    service: ChatService = Depends(get_chat_service),
) -> StreamingResponse:
    if body.conversation_id:
        convo = await service.get_conversation(session, body.conversation_id)
        if convo is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        convo = await service.create_conversation(session)

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for ev in service.stream_reply(
                session,
                conversation=convo,
                user_message=body.message,
                role=body.role,
            ):
                yield _sse(ev.kind, ev.data)
        except Exception as exc:  # noqa: BLE001 - the stream must always close cleanly
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
