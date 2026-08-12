"""Central configuration.

Everything the operator can tune lives here and is sourced from the
environment / `.env`.  No module elsewhere in the codebase should read
`os.environ` directly.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent

ModelRole = Literal["large", "fast", "router", "embed"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Lets the settings service pass overrides by field name
        # (cors_origins_raw) rather than by env alias (CORS_ORIGINS).
        populate_by_name=True,
    )

    # --- Ollama ------------------------------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_embed_base_url: Optional[str] = None
    ollama_timeout: float = 300.0
    ollama_keep_alive: str = "5m"

    # --- Models ------------------------------------------------------------
    llm_model_large: str = "gemma3:27b"
    llm_model_fast: str = "gemma3:12b"
    llm_model_router: str = "gemma3:12b"
    embedding_model: str = "qwen3-embedding:4b"

    # --- Context -----------------------------------------------------------
    llm_context_large: int = 32768
    llm_context_fast: int = 8192
    llm_context_router: int = 8192
    llm_generation_buffer: int = 2048
    llm_temperature: float = 0.3

    # Per-role temperature overrides. Blank/None falls back to llm_temperature.
    # The router is pinned to 0.0 in code: it emits JSON, not prose.
    llm_temperature_large: Optional[float] = None
    llm_temperature_fast: Optional[float] = None

    context_safety_margin: int = 200
    max_history_turns: int = 20

    # --- Retrieval (Milestone 3 — stored now, applied when M3 lands) -------
    retrieval_top_k: int = 8
    retrieval_dense_threshold: float = 0.5
    retrieval_max_chars_per_source: int = 8000
    chunk_target_chars: int = 1200
    chunk_ceiling_chars: int = 1600
    chunk_overlap_sentences: int = 2
    hybrid_weight_keyword: float = 1.0
    hybrid_weight_semantic: float = 1.0
    hybrid_weight_recency: float = 0.5
    hybrid_weight_importance: float = 0.5

    # --- Sync (Milestones 2, 6, 7) ----------------------------------------
    # Live-only by design: a watermark is recorded when an account connects and
    # only what arrives after it is ingested. See docs/ARCHITECTURE.md §2.
    gmail_poll_interval_seconds: int = 60
    #: Bounded one-off bootstrap so day one is not empty. 0 = pure live.
    seed_window_days: int = 0
    seed_max_messages: int = 500

    # A calendar's value is in the future, so it syncs a window rather than a
    # watermark — otherwise tomorrow's meeting, created last week, is invisible.
    calendar_poll_interval_seconds: int = 300
    calendar_past_days: int = 7
    calendar_future_days: int = 90

    # iMessage: consecutive messages closer together than this are one session,
    # which is the unit that gets summarised and embedded.
    imessage_session_gap_minutes: int = 30
    imessage_poll_interval_seconds: int = 30

    # --- Mail scope (Milestone 2) -----------------------------------------
    # Default keeps INBOX + SENT and drops the Promotions/Social/Forums tabs.
    # CATEGORY_UPDATES is deliberately kept: bills and shipping notices live
    # there and carry real deadlines.
    gmail_label_mode: str = "default"          # default | all_mail | custom
    gmail_include_labels: str = "INBOX,SENT"
    gmail_exclude_labels: str = "CATEGORY_PROMOTIONS,CATEGORY_SOCIAL,CATEGORY_FORUMS"

    # Deleting in Gmail deletes locally, cascading to chunks, embeddings,
    # commitments, and downloaded attachment files on disk.
    mirror_upstream_deletions: bool = True

    # --- Attachments (Milestone 2/3) --------------------------------------
    attachment_download: bool = True
    attachment_max_mb: float = 10.0
    attachment_index_text: bool = True
    attachment_ocr_scanned: bool = False
    attachment_total_budget_gb: float = 5.0
    attachment_retention_days: int = 0          # 0 = keep while the message exists

    # --- Extraction (Milestone 3) -----------------------------------------
    # Promises you made ("I'll send it Friday") are tracked and can remind you.
    # A higher bar than incoming commitments, so a throwaway "I'll take a look"
    # does not become a nag.
    track_own_promises: bool = True
    remind_own_promises: bool = True
    own_promise_min_confidence: float = 0.60

    identity_merge_policy: str = "always_ask"
    triage_audit_rate: float = 0.05
    importance_prior_weight: float = 0.45
    extraction_min_confidence: float = 0.35
    extraction_require_evidence: bool = True
    embed_min_tokens: int = 15
    embed_automated_messages: bool = False
    user_timezone: str = "UTC"

    # --- Classification & notifications (Milestone 6) ---------------------
    classify_new_messages: bool = True
    notify_importance_threshold: float = 0.7
    notify_requires_action_only: bool = True
    notify_max_per_hour: int = 6
    notify_quiet_hours_start: int = 22
    notify_quiet_hours_end: int = 8
    notify_browser: bool = True
    notify_macos: bool = False

    # --- Interface --------------------------------------------------------
    ui_default_role: str = "large"
    ui_show_context_meter: bool = True
    ui_show_tool_calls: bool = True
    ui_stream_responses: bool = True

    # --- Storage -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/gary.db"
    data_dir: Path = Path("./data")

    # --- Server ------------------------------------------------------------
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    api_auth_token: Optional[str] = None
    # Kept as a raw string: pydantic-settings JSON-decodes list-typed fields
    # from .env before validators run, which rejects plain comma-separated
    # values. Parsed by the `cors_origins` property below.
    cors_origins_raw: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        alias="CORS_ORIGINS",
    )
    log_level: str = "INFO"

    # --- Google (Milestone 2) ---------------------------------------------
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    google_redirect_uri: str = "http://127.0.0.1:8000/api/auth/google/callback"
    credential_encryption_key: Optional[str] = None

    # ---------------------------------------------------------------- validators
    @field_validator("ollama_base_url", "ollama_embed_base_url", mode="before")
    @classmethod
    def _normalise_url(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().rstrip("/")
            return v or None
        return v

    @field_validator("api_auth_token", "credential_encryption_key", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _refuse_unauthenticated_network_exposure(self) -> "Settings":
        """Binding to anything but loopback without a token is a foot-gun.

        This app will hold the full text of your email. Refuse to start rather
        than quietly serve it to the LAN.
        """
        loopback = {"127.0.0.1", "localhost", "::1"}
        if self.app_host not in loopback and not self.api_auth_token:
            raise ValueError(
                f"APP_HOST is {self.app_host!r} (non-loopback) but API_AUTH_TOKEN is empty. "
                "Set API_AUTH_TOKEN to expose Gary beyond localhost, or bind to 127.0.0.1."
            )
        return self

    # ---------------------------------------------------------------- helpers
    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    @property
    def embed_base_url(self) -> str:
        """Embeddings may live on a different host than chat."""
        return self.ollama_embed_base_url or self.ollama_base_url

    @property
    def resolved_data_dir(self) -> Path:
        p = self.data_dir
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    def model_for(self, role: ModelRole) -> str:
        return {
            "large": self.llm_model_large,
            "fast": self.llm_model_fast,
            "router": self.llm_model_router,
            "embed": self.embedding_model,
        }[role]

    def context_for(self, role: ModelRole) -> int:
        return {
            "large": self.llm_context_large,
            "fast": self.llm_context_fast,
            "router": self.llm_context_router,
            "embed": self.llm_context_fast,
        }[role]

    def temperature_for(self, role: ModelRole) -> float:
        """Per-role sampling temperature.

        The router is pinned to 0.0: it produces JSON for a schema, where
        creativity is a defect rather than a feature.
        """
        if role == "router":
            return 0.0
        override = {
            "large": self.llm_temperature_large,
            "fast": self.llm_temperature_fast,
        }.get(role)
        return self.llm_temperature if override is None else override

    def base_url_for(self, role: ModelRole) -> str:
        return self.embed_base_url if role == "embed" else self.ollama_base_url

    def encryption_key_bytes(self) -> Optional[bytes]:
        """Decoded CREDENTIAL_ENCRYPTION_KEY, validated to be 32 bytes."""
        if not self.credential_encryption_key:
            return None
        raw = base64.urlsafe_b64decode(self.credential_encryption_key)
        if len(raw) != 32:
            raise ValueError("CREDENTIAL_ENCRYPTION_KEY must decode to exactly 32 bytes")
        return raw

    def sqlite_path(self) -> Optional[Path]:
        """Filesystem path behind a sqlite DATABASE_URL, if it is one."""
        url = self.database_url
        for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
            if url.startswith(prefix):
                tail = url[len(prefix) :]
                if tail == ":memory:":
                    return None
                p = Path(tail)
                return p if p.is_absolute() else (REPO_ROOT / p).resolve()
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
