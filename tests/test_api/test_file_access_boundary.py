"""File entry-point security checks with synthetic data and in-process transports."""

import base64
import json

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.mcp.operation_args import ExecuteArgsUnion, QueryArgsUnion
from pydantic import TypeAdapter


@pytest.fixture
def server():
    # Defer importing the process-global MCP registry until test execution.
    # MCP suite collection configures its legacy-tool mode in its conftest.
    from rka.mcp import server

    return server


@pytest_asyncio.fixture
async def file_boundary(db, tmp_path, monkeypatch, server):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "allowed.txt").write_text("Allowed synthetic research note", encoding="utf-8")
    sentinel = tmp_path / "private.txt"
    sentinel.write_text("PRIVATE-SYNTHETIC-SENTINEL", encoding="utf-8")
    monkeypatch.delenv("RKA_HOST_FILE_ROOTS", raising=False)

    def client(*, enabled=False):
        config = RKAConfig(
            data_dir=tmp_path / "data",
            db_path=tmp_path / "unused.db",
            embeddings_enabled=False,
            llm_enabled=False,
            server_file_roots=[inbox] if enabled else [],
        )
        app = create_app(config)
        app.state.db = db
        app.state.config = config
        app.state.llm = None
        app.state.embeddings = None
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://isolated.test",
            headers={"X-RKA-Project": "proj_default"},
        )

    monkeypatch.setattr(server, "_client", lambda project_id=None: client())
    return client, inbox, sentinel


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["sources", "artifacts", "workspace/scan"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_server_paths_are_not_authorized_by_request(file_boundary, endpoint, enabled, db):
    client, inbox, sentinel = file_boundary
    path = sentinel if enabled else inbox / "allowed.txt"
    if endpoint == "sources":
        payload = {"source_kind": "file", "filepath": str(path), "registered_by": "system"}
    elif endpoint == "artifacts":
        payload = {"filepath": str(path), "created_by": "system"}
    else:
        payload = {"folder_path": str(path.parent), "use_llm": False}
    # Request fields cannot override the operator configuration.
    if endpoint != "sources":  # Source input already forbids unknown fields.
        payload["server_file_roots"] = [str(path.parent)]
    async with client(enabled=enabled) as api:
        response = await api.post(f"/api/{endpoint}", json=payload)
    assert response.status_code == 403
    assert response.json()["error"] in {"file_access_disabled", "file_access_denied"}
    assert "PRIVATE-SYNTHETIC-SENTINEL" not in response.text
    assert (await db.fetchone("SELECT count(*) AS n FROM artifacts"))["n"] == 0


@pytest.mark.asyncio
async def test_server_paths_disabled_does_not_block_byte_registration(file_boundary):
    client, _, _ = file_boundary
    async with client() as api:
        response = await api.post(
            "/api/sources",
            json={
                "source_kind": "file",
                "registered_by": "executor",
                "filename": "uploaded.txt",
                "content_base64": base64.b64encode(b"explicitly supplied bytes").decode(),
            },
        )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_forged_workspace_manifest_cannot_escape_allowed_root(file_boundary, db):
    client, inbox, sentinel = file_boundary
    async with client(enabled=True) as api:
        response = await api.post(
            "/api/workspace/ingest",
            json={
                "manifest": {
                    "scan_id": "scn_forged",
                    "root_path": str(inbox),
                    "total_files_found": 1,
                    "total_files_scanned": 1,
                    "files": [
                        {
                            "path": str(sentinel),
                            "relative_path": "../private.txt",
                            "filename": "private.txt",
                            "extension": ".txt",
                            "size_bytes": 1,
                            "category": "text",
                            "ingestion_target": "journal_entry",
                            "file_hash": "forged",
                            "proposed_type": "note",
                        }
                    ],
                },
                "source": "executor",
            },
        )
    assert response.status_code == 403
    assert (await db.fetchone("SELECT count(*) AS n FROM journal"))["n"] == 0


def test_host_source_path_requires_operator_roots(file_boundary, server):
    _, inbox, _ = file_boundary
    with pytest.raises(PermissionError, match="RKA_HOST_FILE_ROOTS"):
        server._read_registered_source_file(str(inbox / "allowed.txt"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool", ["rka_scan_workspace_tree", "rka_scan_workspace", "rka_bootstrap_workspace"]
)
async def test_host_workspace_tools_require_operator_roots(file_boundary, tool, server):
    _, inbox, _ = file_boundary
    with pytest.raises(PermissionError, match="RKA_HOST_FILE_ROOTS"):
        await getattr(server, tool)(folder_path=str(inbox), project_id="proj_default")


@pytest.mark.asyncio
async def test_server_allowlist_does_not_grant_host_access(file_boundary, monkeypatch, server):
    _, inbox, _ = file_boundary
    monkeypatch.setenv("RKA_SERVER_FILE_ROOTS", json.dumps([str(inbox)]))
    with pytest.raises(PermissionError):
        server._read_registered_source_file(str(inbox / "allowed.txt"))


@pytest.mark.asyncio
async def test_allowed_server_scan_and_ingest_preserve_content(file_boundary, db):
    client, inbox, _ = file_boundary
    async with client(enabled=True) as api:
        scanned = await api.post(
            "/api/workspace/scan", json={"folder_path": str(inbox), "use_llm": False}
        )
        assert scanned.status_code == 200, scanned.text
        manifest = scanned.json()
        assert manifest["files"][0]["path"] == str(inbox / "allowed.txt")
        response = await api.post(
            "/api/workspace/ingest", json={"manifest": manifest, "source": "executor"}
        )
    assert response.status_code == 200, response.text
    assert response.json()["total_created"] == 1
    assert (await db.fetchone("SELECT content FROM journal"))[
        "content"
    ] == "Allowed synthetic research note"


@pytest.mark.asyncio
async def test_changed_file_requires_rescan_without_creating_record(file_boundary, db):
    client, inbox, _ = file_boundary
    async with client(enabled=True) as api:
        scanned = await api.post(
            "/api/workspace/scan", json={"folder_path": str(inbox), "use_llm": False}
        )
        (inbox / "allowed.txt").write_text("changed after scan", encoding="utf-8")
        response = await api.post("/api/workspace/ingest", json={"manifest": scanned.json()})
    assert response.status_code == 200
    assert response.json()["total_errors"] == 1
    assert response.json()["total_created"] == 0
    assert "File changed since scan" in response.json()["results"][0]["error"]
    assert (await db.fetchone("SELECT count(*) AS n FROM journal"))["n"] == 0


@pytest.mark.asyncio
async def test_batch_changed_file_reports_earlier_success(file_boundary, db):
    client, inbox, _ = file_boundary
    other = inbox / "later.txt"
    other.write_text("later synthetic note", encoding="utf-8")
    async with client(enabled=True) as api:
        scanned = await api.post(
            "/api/workspace/scan", json={"folder_path": str(inbox), "use_llm": False}
        )
        manifest = scanned.json()
        manifest["files"].sort(key=lambda item: item["filename"])
        other.write_text("changed after scan", encoding="utf-8")
        response = await api.post(
            "/api/workspace/ingest", json={"manifest": manifest, "source": "executor"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["total_created"] == body["total_errors"] == 1
    assert body["results"][0]["entity_ids"]
    assert "File changed" in body["results"][1]["error"]
    assert (await db.fetchone("SELECT count(*) AS n FROM journal"))["n"] == 1


@pytest.mark.asyncio
async def test_pdf_ingest_keeps_original_locator_not_snapshot(file_boundary, db):
    fitz = pytest.importorskip("fitz")
    client, inbox, _ = file_boundary
    pdf = inbox / "paper.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "Synthetic file boundary test")
        document.save(pdf)
    async with client(enabled=True) as api:
        scanned = await api.post(
            "/api/workspace/scan", json={"folder_path": str(inbox), "use_llm": False}
        )
        response = await api.post(
            "/api/workspace/ingest", json={"manifest": scanned.json(), "source": "executor"}
        )
    assert response.status_code == 200, response.text
    assert response.json()["total_errors"] == 0, response.text
    assert (await db.fetchone("SELECT pdf_path FROM literature"))["pdf_path"] == str(pdf)


@pytest.mark.asyncio
async def test_workspace_content_checks_utf8_bytes_before_any_write(file_boundary, db):
    from rka.infra.file_access import MAX_TEXT_BYTES

    client, _, _ = file_boundary
    async with client() as api:
        response = await api.post(
            "/api/workspace/ingest/with-content",
            json={
                "scan_id": "scn_synthetic",
                "relative_path": "note.txt",
                "filename": "note.txt",
                "content": "中" * (MAX_TEXT_BYTES // 3 + 1),
                "content_type": "text",
                "source": "executor",
            },
        )
    assert response.status_code == 413
    assert (await db.fetchone("SELECT count(*) AS n FROM journal"))["n"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["sources", "artifacts"])
async def test_allowed_server_file_registration(file_boundary, endpoint):
    client, inbox, _ = file_boundary
    payload = {"filepath": str(inbox / "allowed.txt")}
    if endpoint == "sources":
        payload.update(source_kind="file", registered_by="executor")
    async with client(enabled=True) as api:
        response = await api.post(f"/api/{endpoint}", json=payload)
    assert response.status_code in (200, 201), response.text


@pytest.mark.asyncio
async def test_typed_host_bootstrap_works_while_server_paths_stay_disabled(
    file_boundary, db, monkeypatch
):
    from rka.mcp import verb_dispatch

    _, inbox, _ = file_boundary
    monkeypatch.setenv("RKA_HOST_FILE_ROOTS", json.dumps([str(inbox)]))
    scan_args = TypeAdapter(QueryArgsUnion).validate_python(
        {
            "operation": "workspace_scan",
            "project_id": "proj_default",
            "filters": {"folder_path": str(inbox)},
        }
    )
    assert "allowed.txt" in await verb_dispatch.dispatch_query_typed(scan_args)
    args = TypeAdapter(ExecuteArgsUnion).validate_python(
        {
            "operation": "bootstrap_workspace",
            "project_id": "proj_default",
            "folder_path": str(inbox),
        }
    )
    assert "Created: 1" in await verb_dispatch.dispatch_execute_typed(args)
    assert (await db.fetchone("SELECT content FROM journal"))[
        "content"
    ] == "Allowed synthetic research note"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative", ["../private.txt", "/private.txt", "C:\\private.txt", "unscanned.txt"]
)
async def test_server_manifest_cannot_nominate_host_files(
    file_boundary, monkeypatch, relative, server
):
    _, inbox, _ = file_boundary
    monkeypatch.setenv("RKA_HOST_FILE_ROOTS", json.dumps([str(inbox)]))
    ingest_calls = []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path == "/api/workspace/scan/from-host":
            files = body["files"]
            assert len(files) == 1
            files[0]["relative_path"] = relative
            return httpx.Response(200, json={"scan_id": "scn_forged", "files": files})
        if request.url.path == "/api/workspace/ingest/with-content":
            ingest_calls.append(body)
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        server,
        "_client",
        lambda project_id=None: httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            base_url="http://mock.test",
        ),
    )
    with pytest.raises(PermissionError, match="unscanned"):
        await server.rka_bootstrap_workspace(str(inbox), project_id="proj_default")
    assert ingest_calls == []


@pytest.mark.asyncio
async def test_bibtex_upload_reads_at_most_limit_plus_one(monkeypatch):
    from rka.api.routes.academic import import_bibtex_file
    from rka.infra.file_access import MAX_TEXT_BYTES, FileAccessError

    sizes = []

    class Upload:
        async def read(self, size=-1):
            sizes.append(size)
            return b"x" * (MAX_TEXT_BYTES + 1)

    with pytest.raises(FileAccessError) as error:
        await import_bibtex_file(file=Upload(), svc=None)
    assert error.value.status_code == 413
    assert sizes == [MAX_TEXT_BYTES + 1]


@pytest.mark.asyncio
async def test_bibtex_service_path_requires_explicit_authority(db, file_boundary, monkeypatch):
    from rka.services.academic import AcademicImportService
    from rka.services.literature import LiteratureService
    from rka.infra.file_access import FileAccessPolicy

    _, inbox, _ = file_boundary
    path = inbox / "refs.bib"
    path.write_text("@article{fixture,title={Allowed}}", encoding="utf-8")
    service = AcademicImportService(LiteratureService(db))
    with pytest.raises(PermissionError):
        await service.import_bibtex_file(str(path))
    service = AcademicImportService(LiteratureService(db), file_policy=FileAccessPolicy([inbox]))
    captured = []

    async def parse(content, **kwargs):
        captured.append(content)
        return {"imported": []}

    monkeypatch.setattr(service, "import_bibtex", parse)
    await service.import_bibtex_file(str(path))
    assert captured == ["@article{fixture,title={Allowed}}"]
