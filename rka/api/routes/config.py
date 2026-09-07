"""Embedding configuration REST endpoints (Mission D T3).

Four routes:

  - GET  /api/config/embedding              — current config (api_key redacted)
  - PUT  /api/config/embedding              — validate + test + persist;
                                              202 if reconciliation runs,
                                              200 if no repair is needed
  - POST /api/config/embedding/test         — probe without persisting
  - GET  /api/config/embedding/backfill/status[?job_id=…]
                                            — polling endpoint for the UI

Error mapping (Affordance G):
  EmbeddingConfigError → 422 {"error": "embedding_config_invalid",
                              "detail": ..., "hint": ...}
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from rka.config import RKAConfig
from rka.infra.embedding_backends import EmbeddingConfigError
from rka.infra.embeddings import EmbeddingService
from rka.services.embedding_jobs import EmbeddingJobs, BackfillScopeBusy, validate_types
from rka.services.embedding_config import (
    EmbeddingConfig,
    EmbeddingConfigService,
)
from rka.services.embedding_index import (
    EmbeddingDimensionTransitionRequired,
    EmbeddingGenerationMismatch,
    assert_online_dimension_compatible,
    embedding_space_signature,
    legacy_index_adoption_safe,
    reconcile_embedding_index,
)

logger = logging.getLogger(__name__)

router = APIRouter()

REDACTED_API_KEY = "***"
_CONFIG_UPDATE_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_service(request: Request) -> EmbeddingConfigService:
    """Per-request service instance pointed at the app's configured /data dir.

    `RKAConfig` has a `data_dir` attribute set during app startup. Tests
    point this at `tmp_path` via the config fixture.
    """
    cfg: RKAConfig = request.app.state.config
    data_dir = getattr(cfg, "data_dir", None) or Path("/data")
    return EmbeddingConfigService(config_dir=data_dir)


def _redact(config: EmbeddingConfig) -> dict[str, Any]:
    """Return a dict copy of `config` with `config.api_key` redacted."""
    payload = config.model_dump()
    sub = payload.get("config") or {}
    if "api_key" in sub and sub["api_key"]:
        sub["api_key"] = REDACTED_API_KEY
        payload["config"] = sub
    return payload


def _restore_saved_secret(
    svc: EmbeddingConfigService,
    body: EmbeddingConfig,
) -> EmbeddingConfig:
    """Replace an omitted/redacted API key with the saved value for probing."""

    try:
        prior = svc.load_config()
    except EmbeddingConfigError:
        return body
    submitted = dict(body.config or {})
    if (
        prior.backend == body.backend
        and (
            "api_key" not in submitted
            or submitted.get("api_key") == REDACTED_API_KEY
        )
    ):
        saved_secret = (prior.config or {}).get("api_key")
        if saved_secret:
            submitted["api_key"] = saved_secret
            return body.model_copy(update={"config": submitted})
    return body


def _snapshot_config_file(svc: EmbeddingConfigService) -> tuple[bytes | None, int | None]:
    """Capture the exact live config so a later DB commit failure can undo save."""

    try:
        data = svc.config_path.read_bytes()
        mode = svc.config_path.stat().st_mode & 0o777
        return data, mode
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise EmbeddingConfigError(
            f"failed to snapshot embedding config at {svc.config_path}: {exc!s}",
            hint="verify the embedding config directory is readable",
        ) from exc


def _restore_config_file(
    svc: EmbeddingConfigService,
    snapshot: tuple[bytes | None, int | None],
) -> None:
    """Atomically restore the exact pre-update config, including absence."""

    data, mode = snapshot
    try:
        if data is None:
            svc.config_path.unlink(missing_ok=True)
            return
        svc.config_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".embedding_config.restore.",
            suffix=".tmp",
            dir=str(svc.config_dir),
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp_path, mode or 0o600)
            os.replace(tmp_path, svc.config_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise EmbeddingConfigError(
            f"failed to restore embedding config at {svc.config_path}: {exc!s}",
            hint="restore embedding_config.backup.json before retrying",
        ) from exc


def _422(error: str, detail: str, hint: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": error, "detail": detail, "hint": hint},
    )


def _space_signature(config: EmbeddingConfig) -> str:
    """Fields that determine the stored embedding space.

    A change here requires a clean rebuild even when dimensions match. Mixing
    vectors from two models or two input contracts in one sqlite-vec table
    makes rankings undefined.
    """
    return embedding_space_signature(config)


def _transport_signature(config: EmbeddingConfig) -> tuple[str, str, str]:
    """Fields that can affect reachability without changing stored vectors."""
    sub = config.config or {}
    base_url = str(sub.get("base_url") or sub.get("host") or "")
    model = str(sub.get("model") or sub.get("model_name") or "")
    return (config.backend, base_url, model)


def _backend_signature(
    config: EmbeddingConfig,
) -> tuple[str, tuple[str, str, str]]:
    """Compatibility wrapper for the complete reconciliation identity.

    Includes `base_url` because repointing a dead backend at a reachable host
    is exactly the recovery action, and it is the one that most needs
    reconciliation: entities created while the old host was unreachable were
    never embedded and nothing else fills them in. Leaving base_url out of the
    signature meant that action returned 200 with no job and the gap persisted
    silently (observed 2026-08-23: 1023 entities, three months, six projects).

    Same dim ⇒ `reshape_all_vec_tables_if_needed` is a no-op, so a base_url
    change costs a backfill of the genuinely-missing rows and nothing else —
    existing vectors are never discarded.

    `api_key` and timeouts are deliberately absent: they may require a client
    refresh, but they do not define a vector space or require re-embedding.
    """
    return (_space_signature(config), _transport_signature(config))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/config/embedding")
async def get_embedding_config(request: Request) -> Any:
    try:
        cfg = _config_service(request).load_config()
    except EmbeddingConfigError as exc:
        return _422("embedding_config_invalid", exc.detail, exc.hint)
    return _redact(cfg)


@router.put("/api/config/embedding")
async def put_embedding_config(
    request: Request,
    body: EmbeddingConfig,
    background_tasks: BackgroundTasks,
    actor: str = Query(default="pi"),
) -> Any:
    async with _CONFIG_UPDATE_LOCK:
        return await _put_embedding_config_locked(
            request,
            body,
            background_tasks,
            actor,
        )


async def _put_embedding_config_locked(
    request: Request,
    body: EmbeddingConfig,
    background_tasks: BackgroundTasks,
    actor: str,
) -> Any:
    svc = _config_service(request)
    try:
        config_snapshot = _snapshot_config_file(svc)
    except EmbeddingConfigError as exc:
        return _422("embedding_config_invalid", exc.detail, exc.hint)

    # Settings never receives the saved secret. Omission (or the GET redaction
    # marker) means preserve the prior key; an explicit empty string removes it.
    body = _restore_saved_secret(svc, body)

    # Step 1: probe the requested config before persisting.
    test_result = await svc.test_config(body)
    if not test_result.ok:
        return _422(
            "embedding_config_invalid",
            f"connection test failed: {test_result.detail}",
            "verify base_url + model + api_key in Settings → Embeddings",
        )

    # Persist the observed dimension. A dimless configuration otherwise writes
    # metadata with dimension 0 and cannot be reconstructed safely at restart.
    normalized_sub = dict(body.config or {})
    if not normalized_sub.get("dim") and test_result.detected_dim:
        normalized_sub["dim"] = int(test_result.detected_dim)
        body = body.model_copy(update={"config": normalized_sub})

    # Step 2: load prior config to determine if backfill is needed.
    try:
        prior = svc.load_config()
    except EmbeddingConfigError:
        # Treat a corrupt prior as "different" so backfill fires.
        prior = None

    space_changed = prior is None or _space_signature(prior) != _space_signature(body)
    transport_changed = prior is None or _transport_signature(prior) != _transport_signature(body)
    prior_key = (prior.config or {}).get("api_key") if prior is not None else None
    credential_changed = prior is None or prior_key != (body.config or {}).get("api_key")
    needs_backfill = space_changed or transport_changed or credential_changed

    db = request.app.state.db
    target_dim = int((body.config or {}).get("dim") or 0)
    try:
        await assert_online_dimension_compatible(db, dim=target_dim)
    except EmbeddingDimensionTransitionRequired as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "embedding_offline_reindex_required",
                "detail": str(exc),
                "hint": "stop RKA API and worker, then run the supervised embedding reindex",
            },
        )

    new_embeddings = EmbeddingService.from_config(body.model_dump(), db=db)

    # Step 3: reconcile and persist under one DB writer lock. The final
    # dimension check is authoritative; a racing old-space write cannot land
    # between it and the sqlite-vec transition. Saving last also means a file
    # error rolls the DB transition back.
    config_saved = False
    queued_job = None
    try:
        async with db.transaction(migration_lock=True):
            await assert_online_dimension_compatible(db, dim=target_dim)
            reconciliation = await reconcile_embedding_index(
                db,
                space_signature=_space_signature(body),
                model_name=new_embeddings.model_name,
                dim=target_dim,
                allow_legacy_adoption=legacy_index_adoption_safe(body),
            )
            new_embeddings.bind_index_generation(
                reconciliation.state.generation, space_signature=reconciliation.state.space_signature,
            )
            needs_backfill = needs_backfill or reconciliation.transitioned or reconciliation.resumed
            if needs_backfill:
                queued_job = await EmbeddingJobs(db).request(new_embeddings)
            saved = svc.save_config(body, actor=actor)
            config_saved = True
    except EmbeddingDimensionTransitionRequired as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "embedding_offline_reindex_required",
                "detail": str(exc),
                "hint": "stop RKA API and worker, then run the supervised embedding reindex",
            },
        )
    except EmbeddingConfigError as exc:
        return _422("embedding_config_invalid", exc.detail, exc.hint)
    except BackfillScopeBusy as exc:
        return JSONResponse(status_code=409, content={"error": "embedding_backfill_busy", "detail": str(exc)})
    except BaseException as exc:  # cancellation must also restore the file
        restore_error = None
        if config_saved:
            try:
                _restore_config_file(svc, config_snapshot)
            except EmbeddingConfigError as restore_exc:
                restore_error = restore_exc.detail
        if not isinstance(exc, Exception):
            raise
        logger.exception("embedding config update failed before commit")
        detail = f"embedding configuration was not committed: {exc!s}"
        if restore_error:
            detail = f"{detail}; rollback also failed: {restore_error}"
        return JSONResponse(
            status_code=500,
            content={
                "error": "embedding_config_update_failed",
                "detail": detail,
                "hint": "retry after checking the RKA database and config volume",
            },
        )

    new_embeddings.bind_index_generation(
        reconciliation.state.generation,
        space_signature=reconciliation.state.space_signature,
    )

    # Swap only after the durable generation transition has committed. Search
    # observes reindexing and stays lexical until the backfill is complete.
    request.app.state.embeddings = new_embeddings
    request.app.state.search.embeddings = new_embeddings
    request.app.state.embedding_unavailable_reason = None

    needs_backfill = (
        needs_backfill
        or reconciliation.transitioned
        or reconciliation.resumed
    )
    if not needs_backfill:
        return JSONResponse(status_code=200, content=_redact(saved))

    # Intent committed atomically above; only a separate Core worker executes it.
    job_id = queued_job["id"]

    return JSONResponse(
        status_code=202,
        content={
            **_redact(saved),
            "job_id": job_id,
            "status_url": f"/api/config/embedding/backfill/status?job_id={job_id}",
            "reshape": {
                table: {"did_reshape": did, "pending": pending}
                for table, (did, pending) in reconciliation.reshape.items()
            },
        },
    )


@router.post("/api/config/embedding/test")
async def test_embedding_config(request: Request, body: EmbeddingConfig) -> Any:
    svc = _config_service(request)
    body = _restore_saved_secret(svc, body)
    try:
        result = await svc.test_config(body)
    except EmbeddingConfigError as exc:
        return _422("embedding_config_invalid", exc.detail, exc.hint)
    return {
        "ok": result.ok,
        "detail": result.detail,
        "detected_dim": result.detected_dim,
        "latency_ms": result.latency_ms,
    }


@router.post("/api/config/embedding/backfill")
async def start_embedding_backfill(
    request: Request,
    background_tasks: BackgroundTasks,
    entity_types: str | None = Query(
        default=None,
        description="comma-separated subset, e.g. 'claim,journal'; omit for all",
    ),
) -> Any:
    """Reconcile missing embeddings without touching the configuration.

    Until this existed the only way to fill a gap was to edit the embedding
    config, which conflates two different intents and — because
    `_backend_signature` ignored base_url — did not even work for the case
    that matters. Backfill only ever adds vectors for entities that have
    none; it never re-embeds or discards existing ones.
    """
    db = request.app.state.db
    embeddings = getattr(request.app.state, "embeddings", None)
    if embeddings is None:
        return _422(
            "embedding_unavailable",
            "no embedding backend is configured",
            "set one in Settings → Embeddings first",
        )
    try:
        if entity_types is not None and len(entity_types) > 128:
            raise ValueError("entity_types exceeds 128 characters")
        types = validate_types(tuple(t.strip() for t in entity_types.split(",")) if entity_types is not None else None)
        job = await EmbeddingJobs(db).request(embeddings, types)
    except ValueError as exc:
        return _422("embedding_entity_types_invalid", str(exc), "use a subset of the seven embedding entity types")
    except BackfillScopeBusy as exc:
        return JSONResponse(status_code=409, content={"error": "embedding_backfill_busy", "detail": str(exc)})
    except EmbeddingGenerationMismatch as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "embedding_generation_changed",
                "detail": str(exc),
                "hint": "restart this RKA process, then retry the backfill",
            },
        )

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job["id"],
            "status_url": f"/api/config/embedding/backfill/status?job_id={job['id']}",
            "entity_types": list(types) if entity_types is not None else "all",
        },
    )


@router.get("/api/config/embedding/backfill/status")
async def get_backfill_status(request: Request, job_id: str | None = Query(default=None)) -> Any:
    """Polling endpoint for the Settings UI progress bar.

    With `job_id`: return that specific job's snapshot. Without: return
    the most recent job (handy for the UI to pick up an in-progress
    backfill after a page refresh).
    """
    status = await EmbeddingJobs(request.app.state.db).status(job_id)
    if status is None and job_id:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    return status or {"state": "idle", "job_id": None}


@router.post("/api/config/embedding/backfill/{job_id}/cancel")
async def cancel_backfill(request: Request, job_id: str) -> Any:
    try:
        return await EmbeddingJobs(request.app.state.db).cancel(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
