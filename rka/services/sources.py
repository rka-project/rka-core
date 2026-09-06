"""Safe external-source registration over the existing artifact substrate."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rka.infra.ids import generate_id
from rka.models.sources import (
    RegisterSourceRequest,
    RegisteredSource,
    RegisteredSourceDetail,
    RegisterSourceResult,
    SourceAdmission,
    SourceAdmissionCreate,
)
from rka.services.base import BaseService, _precise_now
from rka.services.interpretation import (
    InterpretationConflictError,
    InterpretationNotFoundError,
)


_SAFE_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REGISTRATION_ACTORS = {
    "pi", "brain", "executor", "web_ui", "llm", "import", "system"
}
_TARGET_TABLES = {"journal": "journal", "claim": "claims", "decision": "decisions"}


class SourceRegistrationError(ValueError):
    """The supplied source cannot be registered safely."""


def verify_registered_source_artifact(
    source: Mapping[str, Any],
    artifact: Mapping[str, Any] | None,
    *,
    project_root: Path,
    expected_content_hash: str | None = None,
) -> None:
    """Fail closed unless a registered source still owns its exact bytes."""

    source_id = str(source.get("id") or "<unknown>")
    if artifact is None or artifact.get("id") != source.get("artifact_id"):
        raise SourceRegistrationError(
            f"registered source {source_id!r} has no managed artifact"
        )
    source_hash = str(source.get("content_hash") or "")
    if expected_content_hash is not None and source_hash != expected_content_hash:
        raise SourceRegistrationError(
            f"registered source {source_id!r} does not match the supplied content hash"
        )
    if artifact.get("content_hash") != source_hash:
        raise SourceRegistrationError(
            f"registered source {source_id!r} and its artifact disagree on content hash"
        )

    raw_path = artifact.get("filepath")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SourceRegistrationError(
            f"registered source {source_id!r} has no managed artifact path"
        )
    path = Path(raw_path).expanduser()
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise SourceRegistrationError(
            f"registered source {source_id!r} managed artifact is missing"
        ) from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise SourceRegistrationError(
            f"registered source {source_id!r} managed artifact is not a regular file"
        )

    root = project_root.expanduser().resolve()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SourceRegistrationError(
            f"registered source {source_id!r} managed artifact is unavailable"
        ) from exc
    if not resolved.is_relative_to(root):
        raise SourceRegistrationError(
            f"registered source {source_id!r} artifact escapes managed project storage"
        )

    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(path, flags)
        with os.fdopen(file_descriptor, "rb") as managed_file:
            opened_stat = os.fstat(managed_file.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise SourceRegistrationError(
                    f"registered source {source_id!r} managed artifact is not regular"
                )
            while chunk := managed_file.read(1024 * 1024):
                digest.update(chunk)
    except SourceRegistrationError:
        raise
    except OSError as exc:
        raise SourceRegistrationError(
            f"registered source {source_id!r} managed artifact cannot be read"
        ) from exc
    if digest.hexdigest() != source_hash:
        raise SourceRegistrationError(
            f"registered source {source_id!r} managed artifact content hash is invalid"
        )


class SourceService(BaseService):
    """Register untrusted sources without silently creating canonical knowledge."""

    def __init__(
        self,
        db,
        llm=None,
        embeddings=None,
        project_id: str = "proj_default",
        *,
        storage_root: Path | None = None,
        max_bytes: int = 50 * 1024 * 1024,
        file_policy=None,
    ):
        super().__init__(db, llm=llm, embeddings=embeddings, project_id=project_id)
        if not _SAFE_PROJECT_ID.fullmatch(project_id) or project_id in {".", ".."}:
            raise SourceRegistrationError(
                "Project ID is not safe for registered-source storage"
            )
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        from rka.infra.file_access import FileAccessPolicy
        self.file_policy = file_policy if file_policy is not None else FileAccessPolicy()
        if storage_root is None:
            if db.db_path == ":memory:" or str(db.db_path).startswith("file:"):
                storage_root = Path(tempfile.gettempdir()) / f"rka-{os.getpid()}-knowledge-packs"
            else:
                storage_root = Path(db.db_path).expanduser().resolve().parent / "knowledge-packs"
        self.storage_root = storage_root.expanduser().resolve()

    async def register(self, data: RegisterSourceRequest) -> RegisterSourceResult:
        """Copy supplied bytes (or a locator manifest) and register provenance.

        No URL is fetched, repository cloned, model called, or canonical entity
        created by this method.
        """
        if data.registered_by not in _REGISTRATION_ACTORS:
            raise SourceRegistrationError("invalid registered_by actor")
        if data.filepath is not None:
            self.file_policy.authorize(data.filepath)
        try:
            provenance_json = self._canonical_json(data.provenance)
        except (TypeError, ValueError) as exc:
            raise SourceRegistrationError(
                "provenance must be a JSON-serializable object"
            ) from exc
        if len(provenance_json.encode("utf-8")) > 100_000:
            raise SourceRegistrationError("provenance exceeds 100000 UTF-8 bytes")

        project_root = self._project_root()
        staging_root = project_root / ".staging"
        staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(staging_root, 0o700)

        staged_path: Path | None = None
        final_path: Path | None = None
        final_dir: Path | None = None
        try:
            staged_path, content_hash, file_size, default_title, filename, mime = (
                self._stage_payload(data, staging_root)
            )
            if data.expected_content_hash and data.expected_content_hash != content_hash:
                raise SourceRegistrationError(
                    "source content hash does not match expected_content_hash"
                )

            title = (data.title or default_title).strip()
            if not title:
                raise SourceRegistrationError("source title must not be blank")
            content_mode = (
                "bytes"
                if any(
                    value is not None
                    for value in (data.filepath, data.pasted_text, data.content_base64)
                )
                else "locator_manifest"
            )
            manifest_payload = {
                "schema_version": "rka.registered-source/v1",
                "source_kind": data.source_kind,
                "content_mode": content_mode,
                "title": title,
                "stable_locator": data.stable_locator,
                "content_hash": content_hash,
                "ownership_kind": data.ownership_kind,
                "ownership_note": data.ownership_note,
                "provenance": data.provenance,
            }
            manifest_hash = hashlib.sha256(
                self._canonical_json(manifest_payload).encode("utf-8")
            ).hexdigest()

            existing = await self._source_row_by_manifest(manifest_hash)
            if existing is not None:
                await self._verify_existing_source(
                    existing,
                    project_root=project_root,
                    expected_content_hash=content_hash,
                )
                return RegisterSourceResult(
                    source=self._row_to_source(existing), duplicate=True
                )

            source_id = generate_id("registered_source")
            artifact_id = generate_id("artifact")
            safe_filename = self._safe_filename(filename)
            final_dir = project_root / "registered-sources" / source_id
            final_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            os.chmod(final_dir, 0o700)
            final_path = (final_dir / safe_filename).resolve()
            if not final_path.is_relative_to(project_root):
                raise SourceRegistrationError("registered source path escapes storage root")
            os.replace(staged_path, final_path)
            staged_path = None
            os.chmod(final_path, 0o600)

            artifact_metadata = {
                "registered_source": True,
                "source_kind": data.source_kind,
                "content_mode": content_mode,
                "stable_locator": data.stable_locator,
                "ownership_kind": data.ownership_kind,
                "provenance": data.provenance,
            }
            try:
                async with self.db.transaction():
                    # Re-check under the database write lock.
                    existing = await self._source_row_by_manifest(manifest_hash)
                    if existing is not None:
                        await self._verify_existing_source(
                            existing,
                            project_root=project_root,
                            expected_content_hash=content_hash,
                        )
                        shutil.rmtree(final_dir, ignore_errors=True)
                        final_path = None
                        return RegisterSourceResult(
                            source=self._row_to_source(existing), duplicate=True
                        )
                    await self.db.execute(
                        """INSERT INTO artifacts (
                               id, filename, filepath, filetype, file_size, mime,
                               content_hash, extraction_status, created_by,
                               metadata, project_id
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                        [
                            artifact_id,
                            safe_filename,
                            str(final_path),
                            Path(filename).suffix.lstrip(".") or None,
                            file_size,
                            mime,
                            content_hash,
                            data.registered_by,
                            self._canonical_json(artifact_metadata),
                            self.project_id,
                        ],
                    )
                    await self.db.execute(
                        """INSERT INTO registered_sources (
                               id, project_id, artifact_id, source_kind,
                               content_mode, title, stable_locator, content_hash,
                               manifest_hash, ownership_kind, ownership_note,
                               provenance, registered_by
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [
                            source_id,
                            self.project_id,
                            artifact_id,
                            data.source_kind,
                            content_mode,
                            title,
                            data.stable_locator,
                            content_hash,
                            manifest_hash,
                            data.ownership_kind,
                            data.ownership_note,
                            provenance_json,
                            data.registered_by,
                        ],
                    )
                    await self.audit(
                        "create",
                        "registered_source",
                        source_id,
                        self._audit_actor(data.registered_by),
                        {
                            "artifact_id": artifact_id,
                            "source_kind": data.source_kind,
                            "manifest_hash": manifest_hash,
                        },
                    )
            except BaseException:
                shutil.rmtree(final_dir, ignore_errors=True)
                final_path = None
                final_dir = None
                raise

            # The database transaction and managed file are now durable. Do not
            # let the outer staging cleanup remove the registered source.
            final_dir = None
            row = await self._source_row(source_id)
            assert row is not None
            return RegisterSourceResult(source=self._row_to_source(row), duplicate=False)
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
            if final_dir is not None:
                shutil.rmtree(final_dir, ignore_errors=True)

    async def get(self, source_id: str) -> RegisteredSourceDetail | None:
        row = await self._source_row(source_id)
        if row is None:
            return None
        artifact = await self.db.fetchone(
            "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
            [row["artifact_id"], self.project_id],
        )
        admissions = await self.db.fetchall(
            """SELECT * FROM source_admissions
               WHERE source_id = ? AND project_id = ?
               ORDER BY created_at, id""",
            [source_id, self.project_id],
        )
        candidate_count = await self.db.fetchone(
            """SELECT COUNT(*) AS count FROM interpretation_candidates
               WHERE project_id = ? AND source_type = 'artifact' AND source_id = ?""",
            [self.project_id, row["artifact_id"]],
        )
        return RegisteredSourceDetail(
            **self._row_to_source(row).model_dump(),
            artifact=dict(artifact) if artifact is not None else {},
            admissions=[self._row_to_admission(item) for item in admissions],
            interpretation_candidate_count=int((candidate_count or {}).get("count") or 0),
        )

    async def list(
        self,
        *,
        source_kind: str | None = None,
        ownership_kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RegisteredSource]:
        conditions = ["project_id = ?"]
        params: list[Any] = [self.project_id]
        if source_kind is not None:
            conditions.append("source_kind = ?")
            params.append(source_kind)
        if ownership_kind is not None:
            conditions.append("ownership_kind = ?")
            params.append(ownership_kind)
        params.extend([limit, offset])
        rows = await self.db.fetchall(
            f"""SELECT * FROM registered_sources
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
            params,
        )
        return [self._row_to_source(row) for row in rows]

    async def admit(
        self,
        source_id: str,
        data: SourceAdmissionCreate,
    ) -> SourceAdmission:
        """Explicitly admit one grounded interpretation to an existing target."""
        async with self.db.transaction():
            source = await self._source_row(source_id)
            if source is None:
                raise InterpretationNotFoundError(
                    f"registered source {source_id!r} not found in this project"
                )
            artifact = await self.db.fetchone(
                "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
                [source["artifact_id"], self.project_id],
            )
            verify_registered_source_artifact(
                source,
                artifact,
                project_root=self._project_root(),
            )

            existing = await self.db.fetchone(
                """SELECT * FROM source_admissions
                   WHERE project_id = ? AND candidate_id = ?""",
                [self.project_id, data.candidate_id],
            )
            if existing is not None:
                if self._is_exact_admission_retry(existing, source_id, data):
                    return self._row_to_admission(existing)
                raise InterpretationConflictError(
                    "interpretation candidate already has a different source admission"
                )

            candidate = await self.db.fetchone(
                """SELECT * FROM interpretation_candidates
                   WHERE id = ? AND project_id = ?""",
                [data.candidate_id, self.project_id],
            )
            if candidate is None:
                raise InterpretationNotFoundError(
                    f"interpretation candidate {data.candidate_id!r} not found"
                )
            if int(candidate["revision"]) != data.expected_revision:
                raise InterpretationConflictError(
                    f"candidate revision is {candidate['revision']}, not expected "
                    f"{data.expected_revision}"
                )
            if candidate["review_status"] not in {"pending", "in_review"}:
                raise InterpretationConflictError(
                    "source admission requires a pending or in_review candidate"
                )
            if candidate["source_type"] != "artifact" or candidate["source_id"] != source["artifact_id"]:
                raise SourceRegistrationError(
                    "candidate must be grounded in this registered source's artifact"
                )

            target_table = _TARGET_TABLES[data.target_type]
            target = await self.db.fetchone(
                f"SELECT id FROM {target_table} WHERE id = ? AND project_id = ?",
                [data.target_id, self.project_id],
            )
            if target is None:
                raise InterpretationNotFoundError(
                    f"{data.target_type} target is not available in this project"
                )

            admission_id = generate_id("source_admission")
            new_revision = int(candidate["revision"]) + 1
            now = _precise_now()
            cursor = await self.db.execute(
                """UPDATE interpretation_candidates
                   SET review_status = 'resolved', disposition = 'promoted',
                       disposition_reason = ?, disposition_target_type = ?,
                       disposition_target_id = ?, reviewed_by = ?, reviewed_at = ?,
                       revision = ?, updated_at = ?
                   WHERE id = ? AND project_id = ? AND revision = ?""",
                [
                    data.reason,
                    data.target_type,
                    data.target_id,
                    data.actor,
                    now,
                    new_revision,
                    now,
                    data.candidate_id,
                    self.project_id,
                    data.expected_revision,
                ],
            )
            if cursor.rowcount != 1:
                raise InterpretationConflictError(
                    "candidate revision changed; reload before admission"
                )
            await self.db.execute(
                """INSERT INTO source_admissions (
                       id, project_id, source_id, candidate_id,
                       candidate_revision, target_type, target_id,
                       source_manifest_hash, actor, reason, grounding_verified
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                [
                    admission_id,
                    self.project_id,
                    source_id,
                    data.candidate_id,
                    new_revision,
                    data.target_type,
                    data.target_id,
                    source["manifest_hash"],
                    data.actor,
                    data.reason,
                ],
            )
            await self.add_link(
                data.target_type,
                data.target_id,
                "derived_from",
                "interpretation_candidate",
                data.candidate_id,
                created_by=data.actor,
            )
            await self.db.execute(
                """INSERT INTO interpretation_review_events (
                       id, project_id, candidate_id, action, from_status,
                       to_status, disposition, actor, reason, target_type,
                       target_id, candidate_revision
                   ) VALUES (?, ?, ?, 'promote', ?, 'resolved', 'promoted',
                             ?, ?, ?, ?, ?)""",
                [
                    generate_id("interpretation_review"),
                    self.project_id,
                    data.candidate_id,
                    candidate["review_status"],
                    data.actor,
                    data.reason,
                    data.target_type,
                    data.target_id,
                    new_revision,
                ],
            )
            await self.audit(
                "create",
                "source_admission",
                admission_id,
                data.actor,
                {
                    "source_id": source_id,
                    "candidate_id": data.candidate_id,
                    "target_type": data.target_type,
                    "target_id": data.target_id,
                    "candidate_revision": new_revision,
                },
            )
        row = await self.db.fetchone(
            "SELECT * FROM source_admissions WHERE id = ? AND project_id = ?",
            [admission_id, self.project_id],
        )
        assert row is not None
        return self._row_to_admission(row)

    def _stage_payload(
        self,
        data: RegisterSourceRequest,
        staging_root: Path,
    ) -> tuple[Path, str, int, str, str, str | None]:
        fd, temp_name = tempfile.mkstemp(prefix="source-", dir=staging_root)
        staged = Path(temp_name)
        os.chmod(staged, 0o600)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as destination:
                if data.filepath is not None:
                    source_path = self.file_policy.authorize(data.filepath)
                    payload = self.file_policy.read_bytes(source_path, max_bytes=self.max_bytes)
                    size = len(payload)
                    digest.update(payload)
                    destination.write(payload)
                    default_title = source_path.name
                    filename = source_path.name
                    mime = data.mime
                else:
                    if data.content_base64 is not None:
                        encoded_limit = 4 * ((self.max_bytes + 2) // 3)
                        if len(data.content_base64) > encoded_limit:
                            raise SourceRegistrationError(
                                f"source exceeds maximum size of {self.max_bytes} bytes"
                            )
                        try:
                            payload = base64.b64decode(
                                data.content_base64.encode("ascii"), validate=True
                            )
                        except (UnicodeEncodeError, binascii.Error) as exc:
                            raise SourceRegistrationError(
                                "content_base64 is not valid canonical base64"
                            ) from exc
                        default_title = data.filename or "Uploaded file"
                        filename = data.filename or "source.bin"
                        mime = data.mime
                    elif data.pasted_text is not None:
                        payload = data.pasted_text.encode("utf-8")
                        default_title = "Pasted text"
                        filename = "pasted.txt"
                        mime = data.mime or "text/plain; charset=utf-8"
                    else:
                        descriptor = {
                            "schema_version": "rka.source-locator/v1",
                            "source_kind": data.source_kind,
                            "title": data.title,
                            "stable_locator": data.stable_locator,
                            "ownership_kind": data.ownership_kind,
                            "ownership_note": data.ownership_note,
                            "provenance": data.provenance,
                        }
                        payload = self._canonical_json(descriptor).encode("utf-8")
                        default_title = data.stable_locator or "Source locator"
                        filename = f"{data.source_kind}-locator.json"
                        mime = data.mime or "application/json"
                    size = len(payload)
                    if size > self.max_bytes:
                        raise SourceRegistrationError(
                            f"source exceeds maximum size of {self.max_bytes} bytes"
                        )
                    digest.update(payload)
                    destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            return staged, digest.hexdigest(), size, default_title, filename, mime
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def _project_root(self) -> Path:
        self.storage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.storage_root, 0o700)
        project_root = (self.storage_root / self.project_id).resolve()
        if not project_root.is_relative_to(self.storage_root):
            raise SourceRegistrationError("project path escapes registered-source storage")
        project_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(project_root, 0o700)
        return project_root

    async def _source_row(self, source_id: str) -> dict | None:
        return await self.db.fetchone(
            "SELECT * FROM registered_sources WHERE id = ? AND project_id = ?",
            [source_id, self.project_id],
        )

    async def _source_row_by_manifest(self, manifest_hash: str) -> dict | None:
        return await self.db.fetchone(
            """SELECT * FROM registered_sources
               WHERE project_id = ? AND manifest_hash = ?""",
            [self.project_id, manifest_hash],
        )

    async def _verify_existing_source(
        self,
        source: Mapping[str, Any],
        *,
        project_root: Path,
        expected_content_hash: str,
    ) -> None:
        artifact = await self.db.fetchone(
            "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
            [source["artifact_id"], self.project_id],
        )
        verify_registered_source_artifact(
            source,
            artifact,
            project_root=project_root,
            expected_content_hash=expected_content_hash,
        )

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @staticmethod
    def _safe_filename(value: str) -> str:
        if not value or "\x00" in value or value in {".", ".."}:
            raise SourceRegistrationError("source filename is not a safe file name")
        if Path(value).name != value or "/" in value or "\\" in value:
            raise SourceRegistrationError("source filename must be a single file name")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SourceRegistrationError("source filename must be valid UTF-8") from exc
        if len(encoded) > 255:
            raise SourceRegistrationError("source filename exceeds 255 UTF-8 bytes")
        return value

    @staticmethod
    def _audit_actor(actor: str) -> str:
        return "system" if actor == "import" else actor

    @staticmethod
    def _row_to_source(row: dict) -> RegisteredSource:
        values = dict(row)
        raw_provenance = values.get("provenance") or "{}"
        try:
            provenance = json.loads(raw_provenance) if isinstance(raw_provenance, str) else raw_provenance
        except json.JSONDecodeError:
            provenance = {}
        values["provenance"] = provenance if isinstance(provenance, dict) else {}
        return RegisteredSource(**values)

    @staticmethod
    def _row_to_admission(row: dict) -> SourceAdmission:
        values = dict(row)
        values["grounding_verified"] = bool(values.get("grounding_verified"))
        return SourceAdmission(**values)

    @staticmethod
    def _is_exact_admission_retry(
        row: dict,
        source_id: str,
        data: SourceAdmissionCreate,
    ) -> bool:
        return (
            row["source_id"] == source_id
            and row["target_type"] == data.target_type
            and row["target_id"] == data.target_id
            and row["actor"] == data.actor
            and row["reason"] == data.reason
            and int(row["candidate_revision"]) == data.expected_revision + 1
            and int(row["grounding_verified"]) == 1
        )
