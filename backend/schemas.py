"""HTTP request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChatTurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    content: str
    model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: Optional[float] = None
    citations: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    archived: bool
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: List[ChatTurnOut] = Field(default_factory=list)


class CreateConversationIn(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32000)
    conversation_id: Optional[str] = None
    # "large" for reasoning, "fast" for quick turns. Overridable per request so
    # the UI can offer a speed/quality toggle without a restart.
    role: str = Field(default="large", pattern="^(large|fast)$")


class ModelInfo(BaseModel):
    role: str
    model: str
    num_ctx: int
    base_url: str
    available: bool
    note: Optional[str] = None


class BackendStatus(BaseModel):
    name: str
    base_url: str
    connected: bool
    version: Optional[str] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    installed_models: List[str] = Field(default_factory=list)


class SourceStatus(BaseModel):
    kind: str
    display_name: str
    status: str
    enabled: bool
    last_sync_at: Optional[datetime] = None
    last_error: Optional[str] = None
    implemented: bool = False


class CountsOut(BaseModel):
    conversations: int = 0
    chat_turns: int = 0
    messages_indexed: int = 0
    threads_indexed: int = 0
    embeddings: int = 0
    pending_jobs: int = 0


class StatusResponse(BaseModel):
    app: str = "gary"
    version: str
    milestone: int
    database_connected: bool
    database_path: Optional[str] = None
    llm_backends: List[BackendStatus] = Field(default_factory=list)
    models: List[ModelInfo] = Field(default_factory=list)
    sources: List[SourceStatus] = Field(default_factory=list)
    counts: CountsOut = Field(default_factory=CountsOut)
    event_subscribers: int = 0
    read_only: bool = True
    uptime_seconds: float = 0.0


class HealthResponse(BaseModel):
    status: str
    version: str
