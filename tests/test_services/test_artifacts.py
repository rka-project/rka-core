"""Artifact service tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from rka.services.artifacts import ArtifactService, build_artifact_text
from rka.infra.file_access import FileAccessPolicy


def test_artifact_embedding_text_canonicalizes_stored_json_metadata() -> None:
    metadata = {"z": 1, "a": 2}

    assert build_artifact_text("result.json", metadata=metadata) == build_artifact_text(
        "result.json",
        metadata='{"z": 1, "a": 2}',
    )


@pytest.mark.asyncio
async def test_register_rejects_invalid_actor_without_partial_write(db, tmp_path: Path):
    path = tmp_path / "artifact.txt"
    path.write_text("artifact", encoding="utf-8")

    svc = ArtifactService(db, file_policy=FileAccessPolicy([tmp_path]))

    with pytest.raises(ValueError, match="Invalid actor 'smoke'"):
        await svc.register(filepath=str(path), filename="artifact.txt", created_by="smoke")

    row = await db.fetchone("SELECT COUNT(*) AS cnt FROM artifacts")
    assert row["cnt"] == 0


@pytest.mark.asyncio
async def test_register_audit_failure_rolls_back_artifact(
    db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "atomic-artifact.txt"
    path.write_text("atomic artifact registration", encoding="utf-8")
    svc = ArtifactService(db, file_policy=FileAccessPolicy([tmp_path]))

    async def fail_audit(*args, **kwargs) -> None:
        raise RuntimeError("simulated artifact audit failure")

    monkeypatch.setattr(svc, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="simulated artifact audit failure"):
        await svc.register(filepath=str(path), created_by="system")

    assert await db.fetchone(
        "SELECT id FROM artifacts WHERE filename = 'atomic-artifact.txt'"
    ) is None


@pytest.mark.asyncio
async def test_register_embeds_after_database_transaction(
    db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "embedded-artifact.txt"
    path.write_text("embed after commit", encoding="utf-8")
    svc = ArtifactService(db, file_policy=FileAccessPolicy([tmp_path]))
    transaction_states: list[tuple[object | None, bool]] = []

    async def observe_embedding(**kwargs) -> None:
        transaction_states.append(
            (db._transaction_owner, db.conn.in_transaction)
        )

    monkeypatch.setattr(svc, "_embed_artifact", observe_embedding)
    result = await svc.register(filepath=str(path), created_by="system")

    assert result["duplicate"] is False
    assert transaction_states == [(None, False)]
    assert await db.fetchone(
        "SELECT id FROM artifacts WHERE id = ?",
        [result["id"]],
    ) is not None


async def _seed_artifact(db, artifact_id: str = "art_atomic") -> None:
    await db.execute(
        """INSERT INTO artifacts
           (id, filename, filepath, filetype, extraction_status, project_id)
           VALUES (?, 'figure.pdf', '/tmp/figure.pdf', 'pdf', 'pending',
                   'proj_default')""",
        [artifact_id],
    )
    await db.commit()


@pytest.mark.asyncio
async def test_store_figure_link_failure_rolls_back_figure(
    db,
    monkeypatch: pytest.MonkeyPatch,
):
    await _seed_artifact(db)
    svc = ArtifactService(db)

    async def fail_link(*args, **kwargs) -> None:
        raise RuntimeError("simulated provenance-link failure")

    monkeypatch.setattr(svc, "add_link", fail_link)

    with pytest.raises(RuntimeError, match="simulated provenance-link failure"):
        await svc._store_figure(
            artifact_id="art_atomic",
            page=1,
            caption="Atomic figure",
            caption_confidence=0.9,
            summary="Must not survive alone",
            claims=[],
        )

    assert await db.fetchone(
        "SELECT id FROM figures WHERE artifact_id = 'art_atomic'"
    ) is None
    assert await db.fetchone(
        """SELECT id FROM entity_links
           WHERE source_id = 'art_atomic' AND link_type = 'produced'"""
    ) is None


@pytest.mark.asyncio
async def test_store_figure_embeds_after_database_transaction(
    db,
    monkeypatch: pytest.MonkeyPatch,
):
    await _seed_artifact(db)
    svc = ArtifactService(db)
    transaction_states: list[tuple[object | None, bool]] = []

    async def observe_embedding(**kwargs) -> None:
        transaction_states.append(
            (db._transaction_owner, db.conn.in_transaction)
        )

    monkeypatch.setattr(svc, "_embed_figure", observe_embedding)
    stored = await svc._store_figure(
        artifact_id="art_atomic",
        page=2,
        caption="Post-commit embedding",
        caption_confidence=0.8,
        summary="Embedding runs outside the write unit",
        claims=[{"claim": "database state is durable first"}],
    )

    assert transaction_states == [(None, False)]
    assert await db.fetchone(
        "SELECT id FROM figures WHERE id = ?",
        [stored["id"]],
    ) is not None
    assert await db.fetchone(
        """SELECT id FROM entity_links
           WHERE source_id = 'art_atomic' AND link_type = 'produced'
             AND target_id = ?""",
        [stored["id"]],
    ) is not None
