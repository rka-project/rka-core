"""Persistent embedding-backend configuration.

The config lives at `/data/embedding_config.json` (the rka-data Docker
volume; survives `docker compose up -d --build`). File-mode 0600 so the
optional `api_key` field is owner-readable only. Every save first writes
the previous config to `/data/embedding_config.backup.json` for one-step
rollback (manual restore — Mission D scope_boundaries says no CLI).

The schema is the same `{"backend": ..., "config": {...}}` shape that
`rka.infra.embedding_backends.make_backend(...)` accepts, plus two
provenance fields (`updated_at`, `updated_by`). T3's REST API redacts the
`api_key` field before returning the config to clients.

First-run behavior (per Mission D acceptance criteria):
  - If the file doesn't exist, `load_config()` returns the default
    FastEmbed-nomic-768 config WITHOUT writing it. T7's startup hook
    persists the default on first boot.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from rka.infra.runtime_lease import RuntimeLease

from rka.infra.embedding_backends import (
    ConnectionTestResult,
    EmbeddingConfigError,
    make_backend,
)

__all__ = [
    "BackendKind",
    "DEFAULT_CONFIG",
    "EmbeddingConfig",
    "EmbeddingConfigError",  # re-exported from rka.infra.embedding_backends.base
    "EmbeddingConfigService",
]

logger = logging.getLogger(__name__)


BackendKind = Literal["openai_compat", "ollama", "fastembed"]


class EmbeddingConfig(BaseModel):
    """Schema for `/data/embedding_config.json`.

    `config` is intentionally a free-form dict because each backend has
    different fields; the runtime check happens when `make_backend` tries
    to instantiate the backend.
    """

    backend: BackendKind
    config: dict[str, Any] = Field(default_factory=dict)
    updated_at: str | None = None
    updated_by: str | None = None

    model_config = {"extra": "forbid"}


DEFAULT_CONFIG = EmbeddingConfig(
    backend="fastembed",
    config={
        "model_name": "nomic-ai/nomic-embed-text-v1.5",
        "dim": 768,
    },
    updated_at=None,
    updated_by="system-default",
)
"""First-run baseline. Used when `/data/embedding_config.json` doesn't
exist yet. T7 startup hook persists this on first boot."""


def _now_iso_z() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EmbeddingConfigService:
    """Read/write the persistent embedding config + run reachability tests.

    Construction takes a `config_dir` so tests can point at `tmp_path`.
    Production binding defaults to `/data` (the rka-data Docker volume).
    """

    CONFIG_FILENAME = "embedding_config.json"
    BACKUP_FILENAME = "embedding_config.backup.json"

    def __init__(self, config_dir: Path | str = Path("/data")) -> None:
        self.config_dir = Path(config_dir)
        self.config_path = self.config_dir / self.CONFIG_FILENAME
        self.backup_path = self.config_dir / self.BACKUP_FILENAME

    # ------------------------------------------------------------------
    # Read / load
    # ------------------------------------------------------------------

    def load_config(self) -> EmbeddingConfig:
        """Return the persisted config; on missing-file, return the default
        WITHOUT persisting (caller decides when to write the default)."""
        if not self.config_path.exists():
            logger.debug(
                "embedding config file missing at %s; returning DEFAULT_CONFIG",
                self.config_path,
            )
            return DEFAULT_CONFIG.model_copy()
        try:
            raw = self.config_path.read_text()
            payload = json.loads(raw)
            return EmbeddingConfig.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            # If the file is corrupt, surface clearly — T3 maps this to
            # 422 embedding_config_invalid. Returning DEFAULT silently
            # would mask the corruption.
            raise EmbeddingConfigError(
                f"embedding config at {self.config_path} is unreadable: {exc!s}",
                hint="restore from /data/embedding_config.backup.json or remove the file",
            )

    # ------------------------------------------------------------------
    # Write / save
    # ------------------------------------------------------------------

    def save_config(self, config: EmbeddingConfig, actor: str) -> EmbeddingConfig:
        with RuntimeLease(self.config_path):
            return self._save_config(config, actor)

    def _save_config(self, config: EmbeddingConfig, actor: str) -> EmbeddingConfig:
        """Persist `config`, after first writing the prior file (if any) to
        `embedding_config.backup.json`. Atomic via tmp+rename. File-mode 0600.

        Returns the persisted config with updated_at + updated_by populated.
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: pre-flight backup. Copy current → backup. If there's no
        # current file, this is a no-op (first-ever save).
        if self.config_path.exists():
            try:
                current = EmbeddingConfig.model_validate_json(
                    self.config_path.read_text()
                )
                if current:
                    shutil.copy2(self.config_path, self.backup_path)
                    # The backup gets the same restrictive mode as the live file.
                    os.chmod(self.backup_path, 0o600)
            except ValueError as exc:
                # A repair must not overwrite a known-good backup with corrupt
                # live bytes. The validated replacement is still written
                # atomically below.
                logger.warning(
                    "embedding config is invalid; preserving existing backup: %s",
                    exc,
                )
            except OSError as exc:
                raise EmbeddingConfigError(
                    f"failed to write pre-flight backup to {self.backup_path}: {exc!s}",
                    hint="verify the /data volume is writable + owner-only",
                )

        # Step 2: stamp provenance.
        stamped = config.model_copy(
            update={
                "updated_at": _now_iso_z(),
                "updated_by": actor or "unknown",
            }
        )

        # Step 3: atomic write via tmp+rename in the same directory.
        body = stamped.model_dump_json(indent=2)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".embedding_config.", suffix=".tmp", dir=str(self.config_dir)
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(body)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.config_path)
        except OSError as exc:
            # If we crashed mid-tmp-write, clean up the tmp so we don't
            # leave a partial file behind. The original config_path is
            # untouched because os.replace is atomic.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise EmbeddingConfigError(
                f"failed to write embedding config to {self.config_path}: {exc!s}",
                hint="verify the /data volume is writable",
            )

        logger.info(
            "embedding config saved (backend=%s; actor=%s)",
            stamped.backend,
            stamped.updated_by,
        )
        return stamped

    # ------------------------------------------------------------------
    # Test (no-persist reachability + dim detection)
    # ------------------------------------------------------------------

    async def test_config(self, config: EmbeddingConfig) -> ConnectionTestResult:
        """Instantiate the backend described by `config` and probe it.

        Does NOT persist or modify any file. Used by the REST endpoint
        `POST /api/config/embedding/test` and by the PUT handler before
        committing.
        """
        try:
            backend = make_backend(config.model_dump())
        except ValueError as exc:
            return ConnectionTestResult(ok=False, detail=f"invalid config: {exc!s}")
        return await backend.test_connection()


# ---------------------------------------------------------------------------
# Errors (re-exported from rka.infra.embedding_backends.base)
# ---------------------------------------------------------------------------
# `EmbeddingConfigError` is imported above. The class is defined in
# `rka/infra/embedding_backends/base.py` so backends can raise it without
# a service-layer import cycle (T2.5 calibration). Re-exporting here keeps
# `from rka.services.embedding_config import EmbeddingConfigError` working
# for callers that landed before the relocation.
