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
