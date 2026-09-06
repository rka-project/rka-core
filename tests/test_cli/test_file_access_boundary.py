"""Local CLI shares operator configuration without granting argv path authority."""

import json
import sqlite3
from contextlib import closing

import pytest
from click.testing import CliRunner

from rka.cli import main
from rka.infra.file_access import FileAccessError


@pytest.mark.parametrize("command", ["scan", "ingest"])
@pytest.mark.parametrize("enabled", [False, True])
def test_bootstrap_cli_uses_operator_roots(tmp_path, monkeypatch, command, enabled):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.txt").write_text("Synthetic CLI note", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RKA_DB_PATH", raising=False)
    monkeypatch.setenv("RKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RKA_EMBEDDINGS_ENABLED", "false")
    monkeypatch.setenv("RKA_LLM_ENABLED", "false")
    monkeypatch.setenv("RKA_HOST_FILE_ROOTS", json.dumps([str(inbox)]))
    monkeypatch.setenv("RKA_SERVER_FILE_ROOTS", json.dumps([str(inbox)] if enabled else []))
    args = ["bootstrap", command, str(inbox), "--no-llm"]
    args += ["--json-output"] if command == "scan" else ["--yes"]
    result = CliRunner().invoke(main, args)
    if not enabled:
        assert isinstance(result.exception, FileAccessError)
        assert "RKA_SERVER_FILE_ROOTS" in str(result.exception)
        assert not (tmp_path / "data" / "rka.db").exists()
    else:
        assert result.exit_code == 0, str(result.exception) + result.output
        if command == "scan":
            assert json.loads(result.output)["files"][0]["path"] == str(inbox / "note.txt")
        else:
            assert "Created: 1" in result.output
            with closing(sqlite3.connect(tmp_path / "data" / "rka.db")) as connection:
                assert (
                    connection.execute("SELECT content FROM journal").fetchone()[0]
                    == "Synthetic CLI note"
                )


@pytest.mark.parametrize("command", ["scan", "ingest"])
def test_cli_releases_database_if_scan_fails(tmp_path, monkeypatch, command):
    from rka.infra.database import Database
    from rka.services.workspace import WorkspaceService

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RKA_DB_PATH", raising=False)
    monkeypatch.setenv("RKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RKA_EMBEDDINGS_ENABLED", "false")
    monkeypatch.setenv("RKA_LLM_ENABLED", "false")
    monkeypatch.setenv("RKA_SERVER_FILE_ROOTS", json.dumps([str(tmp_path)]))
    closed = []
    original_close = Database.close

    async def close(self):
        await original_close(self)
        closed.append(True)

    async def fail(*args, **kwargs):
        raise FileAccessError("Synthetic scan failure")

    monkeypatch.setattr(Database, "close", close)
    monkeypatch.setattr(WorkspaceService, "scan", fail)
    args = ["bootstrap", command, str(tmp_path), "--no-llm"]
    if command == "ingest":
        args.append("--yes")
    result = CliRunner().invoke(main, args)
    assert isinstance(result.exception, FileAccessError)
    assert closed == [True]
