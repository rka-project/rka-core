"""Embedding-backend Protocol + shared result types + shared errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class EmbeddingConfigError(Exception):
    """Surface for embedding-config + runtime invariant failures.

    Two failure classes both surface here:

      1. **Static config failures** — corrupt `/data/embedding_config.json`,
         unknown backend kind, missing required field. Raised from the
         service layer (`rka/services/embedding_config.py`).

      2. **Runtime dim-drift** — a backend's `embed()` observed a vector
         whose length doesn't match the configured `dim`. This is the
         Mission-D T2.5 calibration fix: silent mutation of `self._dim`
         from inside `embed()` was contradicting the Protocol docstring
         ("Embedding dimensionality. Stable for the lifetime of the
         instance.") and letting on-disk-config ↔ in-memory-backend ↔
         vec_claims-table-dim drift silently.

    Both classes carry `detail` + `hint`. T3's REST handlers map this to
    a 422 `embedding_config_invalid` response with the same structured
    body shape used by Mission B Affordance G.
    """

    def __init__(self, detail: str, *, hint: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.hint = hint


def reconcile_dim(self_dim: int, observed_dim: int) -> int:
    """Unified dim-check rule applied by every backend in production paths.

    Returns the dim the backend should now report. Two cases:

      - `self_dim == 0` (constructed without an expected dim, or
        `test_connection()` from an empty-dim baseline) → return
        `observed_dim` so the caller can populate `self._dim`. This is
        the legitimate "populate from zero" path the Brain ratified at
        the mid-mission gate.

      - `self_dim > 0` and `observed_dim != self_dim` → raise
        `EmbeddingConfigError`. The configured dim is now contradicted
        by reality (user mistyped dim, swapped the model server-side
        without updating config, etc.). Raising surfaces the bug at the
        site of the divergence, far away from the otherwise-confusing
        sqlite-vec insert-fail.

      - `self_dim > 0` and matches → return `self_dim` unchanged.
    """
    if self_dim == 0:
        return observed_dim
    if observed_dim != self_dim:
        raise EmbeddingConfigError(
            f"dim mismatch: configured={self_dim}, server={observed_dim}",
            hint="update embedding config via Settings → Embeddings",
        )
    return self_dim


@dataclass
class ConnectionTestResult:
    """Result of `EmbeddingBackend.test_connection()`.

    `ok` is the headline; `detail` is a one-line human-readable summary.
    `detected_dim` and `latency_ms` are populated when the backend can
    measure them (a successful test embed call returns both).
    """

    ok: bool
    detail: str
    detected_dim: int | None = None
    latency_ms: float | None = None


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Protocol every embedding backend implements.

    Async because all three concrete backends either do I/O (HTTP) or
    blocking work in a bounded native executor (FastEmbed). The
    `is_query` flag lets backends that distinguish query- vs document-
    encoding (Nomic does via the `search_query:` / `search_document:`
    prefix) honor that; HTTP backends typically ignore it.
    """

    @property
    def dim(self) -> int:
        """Embedding dimensionality. Stable for the lifetime of the instance."""
        ...

    @property
    def model_name(self) -> str:
        """Backend-specific model identifier (for logging + provenance)."""
        ...

    async def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        """Embed a single string. Returns a `dim`-length float list."""
        ...

    async def embed_batch(
        self, texts: list[str], *, is_query: bool = False
    ) -> list[list[float]]:
        """Embed many strings. Length-N input → length-N output."""
        ...

    async def test_connection(self) -> ConnectionTestResult:
        """Cheap reachability + dim-detection probe; never persists state."""
        ...
