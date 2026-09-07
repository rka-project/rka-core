"""RKA configuration via pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from functools import cached_property

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_data_dir() -> Path:
    """Pick the local data directory: explicit override, then ``~/.rka``.

    Docker sets ``RKA_DATA_DIR=/data`` explicitly. The mere presence of a
    host ``/data`` directory is not a reliable signal that RKA is running in
    a container.
    """
    import os

    explicit = os.environ.get("RKA_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home().expanduser() / ".rka"


class RKAConfig(BaseSettings):
    """Configuration loaded from .env / environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="RKA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Project
    project_dir: Path = Field(default=Path("."), description="Project root directory")
    db_path: Path | None = Field(
        default=None,
        description=(
            "Optional SQLite database override. When unset, the path is resolved "
            "under data_dir; an explicit relative value remains relative to "
            "project_dir for legacy project-local deployments."
        ),
    )
    # Persistent data directory. Houses `rka.db`, `embedding_config.json`,
    # and other persistent state. Tests override via `RKAConfig(data_dir=tmp_path)`.
    # Resolution:
    #   1. RKA_DATA_DIR env var (explicit override)
    #   2. ~/.rka (Dockerless fallback — created on first use)
    # Docker explicitly supplies RKA_DATA_DIR=/data.
    data_dir: Path = Field(
        default_factory=lambda: _resolve_data_dir(),
        validate_default=True,
        description="Persistent data directory",
    )

    @field_validator("data_dir", mode="after")
    @classmethod
    def _prepare_data_dir(cls, value: Path) -> Path:
        """Normalize and create the private persistent-state directory."""
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("RKA_DATA_DIR must be an absolute path")
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"cannot create RKA_DATA_DIR {path}: {exc}") from exc
        return path.resolve()

    # Server
    host: str = Field(default="127.0.0.1", description="API server host")
    port: int = Field(default=9712, description="API server port")

    # LLM — configured at runtime from Settings page or env vars.
    llm_model: str = Field(default="", description="LiteLLM model identifier")
    llm_api_base: str | None = Field(default=None, description="LLM API base URL")
    llm_api_key: str | None = Field(default=None, description="Optional API key")
    llm_enabled: bool = Field(
        default=False,
        description="Enable frozen legacy LLM compatibility features",
    )
    llm_think: bool = Field(
        default=False,
        description="Enable thinking mode for reasoning models (disable for structured extraction)",
    )

    # LLM context window — auto-detected from backend, or set manually
    llm_context_window: int = Field(default=0, description="Model context window in tokens (0 = unknown/auto)")
    llm_request_timeout: int = Field(default=120, description="Timeout for LLM requests in seconds")

    # Manuscript workbench local suggestion adapter. This is intentionally
    # separate from the legacy general-purpose LLM settings: semantic proposal
    # generation is local-machine-only and never receives a cloud credential.
    workbench_lm_studio_base_url: str = Field(
        default="http://127.0.0.1:1234/v1",
        description="Local-machine OpenAI-compatible LM Studio base URL",
    )
    workbench_lm_studio_model: str = Field(
        default="",
        description="LM Studio model id used for semantic patch suggestions",
    )
    workbench_lm_studio_timeout: int = Field(
        default=120,
        ge=1,
        le=600,
        description="LM Studio proposal request timeout in seconds",
    )

    # Manuscript source synchronization is deliberately opt-in. A
    # manuscript.workspace_ref never grants filesystem authority on its own;
    # it must resolve below one of these os.pathsep-separated roots.
    manuscript_workspace_roots: str = Field(
        default="",
        description=(
            "os.pathsep-separated allowlist of local manuscript workspace roots"
        ),
    )
    manuscript_source_max_bytes: int = Field(
        default=2 * 1024 * 1024,
        ge=1,
        le=20 * 1024 * 1024,
        description="Maximum UTF-8 bytes in one synchronized manuscript source file",
    )

    registered_source_max_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1,
        le=500 * 1024 * 1024,
        description="Maximum bytes copied into one registered source artifact",
    )

    # Operator configuration, never accepted from API payloads or manifests.
    # JSON lists avoid ambiguous ':' drive separators on Windows.
    server_file_roots: list[Path] = Field(
        default_factory=list,
        description="Authorized server-side import directories; empty disables path reads",
    )

    @cached_property
    def file_access_policy(self):
        from rka.infra.file_access import FileAccessPolicy
        return FileAccessPolicy(self.server_file_roots, max_bytes=self.registered_source_max_bytes)

    # Embeddings are optional in the base Python distribution. Docker and a
    # future full-profile installer explicitly enable them after installing
    # the embeddings extra. Persistent backend config lives under data_dir.
    embedding_model: str = Field(
        default="nomic-ai/nomic-embed-text-v1.5", description="FastEmbed model"
    )
    embeddings_enabled: bool = Field(default=False, description="Enable embedding generation")

    # Context Engine — v2.4 (dec_01KQQPD6Y6B362T3K08368BDMP) removed temperature
    # bucketing and token-budget arithmetic. Ranking is SQL-time importance ×
    # centrality × recency. The former env vars (RKA_CONTEXT_HOT_DAYS,
    # RKA_CONTEXT_WARM_DAYS, RKA_CONTEXT_DEFAULT_MAX_TOKENS) were removed.

    # Background jobs
    job_poll_interval: float = Field(default=1.0, description="Worker poll interval in seconds")
    job_lease_seconds: int = Field(default=300, description="Job lease duration before recovery")
    job_max_attempts: int = Field(default=5, description="Max attempts before a job is marked failed")

    @property
    def database_url(self) -> str:
        """Return the one authoritative SQLite path for this configuration.

        An absent ``RKA_DB_PATH`` uses ``data_dir/rka.db`` so commands launched
        from different working directories see the same Core. Explicit
        relative paths preserve the legacy ``project_dir`` behavior used by
        ``rka init`` and existing project-local ``.env`` files.
        """
        if self.db_path is None:
            return str(self.data_dir / "rka.db")

        db = self.db_path
        if not db.is_absolute():
            db = self.project_dir / db
        return str(db)
