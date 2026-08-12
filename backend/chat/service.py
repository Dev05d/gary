"""Conversation persistence and streaming orchestration.

This is the seam the agent loop plugs into at Milestone 5. Today it goes
straight to the LLM with no retrieval; from M5 it will first run the tool loop
and pass the results in as fenced `UntrustedDocument`s. The prompt assembly and
trust boundary already assume that shape, so wiring retrieval in does not
require rewriting this module.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.database.models import ChatTurn, Conversation, utcnow
from backend.llm.base import ChatMessage, LLMUnavailableError
from backend.llm.context_budget import BudgetedItem, build_prompt, measure
from backend.llm.registry import LLMRegistry
from backend.security.prompt_guard import UntrustedDocument, build_system_prompt, wrap_document

log = logging.getLogger(__name__)

PERSONA = """\
You are Gary, a private assistant running entirely on the user's own hardware.
You help them understand their email, messages, calendar, and files.

Style: direct and concise. Lead with the answer. Use Markdown. Do not pad
replies with restatements of the question or offers to help further.
"""

#: Turns kept in the prompt before older ones are evicted by the budgeter.
MAX_HISTORY_TURNS = 20


@dataclass
class StreamEvent:
    """What the API layer serialises to SSE."""

    kind: str  # start | token | done | error
    data: dict


class ChatService:
    def __init__(self, registry: LLMRegistry, settings: Settings) -> None:
        self._registry = registry
        self._settings = settings

    # ------------------------------------------------------------ conversations
    async def create_conversation(
        self, session: AsyncSession, title: Optional[str] = None
    ) -> Conversation:
        convo = Conversation(title=title or "New conversation")
        session.add(convo)
        await session.commit()
        await session.refresh(convo)
        return convo

    async def get_conversation(
        self, session: AsyncSession, conversation_id: str
    ) -> Optional[Conversation]:
        return await session.get(Conversation, conversation_id)

    async def list_conversations(
        self, session: AsyncSession, *, limit: int = 100, include_archived: bool = False
    ) -> Sequence[Conversation]:
        stmt = select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
        if not include_archived:
            stmt = stmt.where(Conversation.archived.is_(False))
        return (await session.execute(stmt)).scalars().all()

    async def delete_conversation(self, session: AsyncSession, conversation_id: str) -> bool:
        convo = await session.get(Conversation, conversation_id)
        if convo is None:
            return False
        await session.delete(convo)
        await session.commit()
        return True

    async def history(
        self, session: AsyncSession, conversation_id: str, *, limit: int = MAX_HISTORY_TURNS
    ) -> List[ChatTurn]:
        stmt = (
            select(ChatTurn)
            .where(ChatTurn.conversation_id == conversation_id)
            .order_by(ChatTurn.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return list(reversed(rows))

    async def counts(self, session: AsyncSession) -> tuple[int, int]:
        convos = await session.scalar(select(func.count()).select_from(Conversation)) or 0
        turns = await session.scalar(select(func.count()).select_from(ChatTurn)) or 0
        return convos, turns

    # ------------------------------------------------------------------- chat
    async def stream_reply(
        self,
        session: AsyncSession,
        *,
        conversation: Conversation,
        user_message: str,
        role: str = "large",
        retrieved: Optional[List[UntrustedDocument]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """Persist the user turn, stream the assistant turn, persist the result."""
        binding = self._registry.binding(role)  # type: ignore[arg-type]

        user_turn = ChatTurn(
            conversation_id=conversation.id, role="user", content=user_message
        )
        session.add(user_turn)
        if conversation.title == "New conversation":
            conversation.title = _derive_title(user_message)
        conversation.updated_at = utcnow()
        await session.commit()

        prior = await self.history(session, conversation.id)
        # history() includes the turn we just wrote; drop it, it becomes the
        # explicit `user_message` at the tail of the prompt.
        prior = [t for t in prior if t.id != user_turn.id]

        async def counter(text: str) -> int:
            return await binding.provider.count_tokens(text, model=binding.model)

        history_items = await measure(
            [ChatMessage(role=t.role, content=t.content) for t in prior],  # type: ignore[arg-type]
            counter,
        )

        retrieved = retrieved or []
        context_items: List[BudgetedItem] = []
        for doc in retrieved:
            block = wrap_document(doc)
            context_items.append(
                BudgetedItem(
                    message=ChatMessage(role="user", content=block),
                    tokens=await counter(block),
                    kind="context",
                    ref=doc.ref,
                )
            )

        system_prompt = build_system_prompt(
            PERSONA, has_retrieved_content=bool(retrieved)
        )

        budget = await build_prompt(
            system_prompt=system_prompt,
            history=history_items,
            context_items=context_items,
            user_message=user_message,
            counter=counter,
            context_limit=binding.num_ctx,
            generation_buffer=self._settings.llm_generation_buffer,
        )

        assistant_turn = ChatTurn(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            model=binding.model,
        )
        session.add(assistant_turn)
        await session.commit()

        yield StreamEvent(
            "start",
            {
                "conversation_id": conversation.id,
                "message_id": assistant_turn.id,
                "user_message_id": user_turn.id,
                "model": binding.model,
                "role": role,
                "context": {
                    "used_tokens": budget.used_tokens,
                    "limit": budget.limit,
                    "utilisation": budget.utilisation,
                    "evicted_history": budget.evicted_history,
                    "evicted_context": budget.evicted_context,
                },
            },
        )
        for note in budget.notes:
            log.info("context budget: %s", note)

        started = time.perf_counter()
        buffer: List[str] = []
        usage = None
        try:
            async for chunk in binding.provider.stream(
                budget.messages,
                model=binding.model,
                num_ctx=binding.num_ctx,
                temperature=self._settings.llm_temperature,
            ):
                if chunk.delta:
                    buffer.append(chunk.delta)
                    yield StreamEvent("token", {"delta": chunk.delta})
                if chunk.done:
                    usage = chunk.usage
        except LLMUnavailableError as exc:
            assistant_turn.error = str(exc)
            assistant_turn.content = "".join(buffer)
            await session.commit()
            yield StreamEvent("error", {"message": str(exc), "message_id": assistant_turn.id})
            return
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            log.exception("chat stream failed")
            assistant_turn.error = f"{type(exc).__name__}: {exc}"
            assistant_turn.content = "".join(buffer)
            await session.commit()
            yield StreamEvent(
                "error", {"message": assistant_turn.error, "message_id": assistant_turn.id}
            )
            return

        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        assistant_turn.content = "".join(buffer)
        assistant_turn.latency_ms = latency_ms
        if usage:
            assistant_turn.prompt_tokens = usage.prompt_tokens
            assistant_turn.completion_tokens = usage.completion_tokens
        if retrieved:
            assistant_turn.citations = {
                "sources": [
                    {
                        "ref": d.ref,
                        "source": d.source,
                        "title": d.title,
                        "author": d.author,
                        "timestamp": d.timestamp,
                    }
                    for d in retrieved
                ]
            }
        conversation.updated_at = utcnow()
        await session.commit()

        yield StreamEvent(
            "done",
            {
                "message_id": assistant_turn.id,
                "latency_ms": latency_ms,
                "usage": {
                    "prompt_tokens": assistant_turn.prompt_tokens,
                    "completion_tokens": assistant_turn.completion_tokens,
                },
                "citations": assistant_turn.citations,
                "title": conversation.title,
            },
        )


def _derive_title(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat or "New conversation"
    return flat[: limit - 1].rstrip() + "…"
