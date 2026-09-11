"""Guard the opt-in synthetic fixture against accidental use on another store."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import rka

from rka.infra.database import Database
from scripts import issue158_legacy_fixture as fixture


@pytest.fixture
def no_database_access(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("fixture must reject this path before opening SQLite")

    monkeypatch.setattr(Database, "connect", forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_root", [None, "/not-the-archived-source"])
async def test_seed_requires_matching_explicit_legacy_source(
    tmp_path, no_database_access, legacy_root
):
    args = SimpleNamespace(operation="seed", data_dir=tmp_path, legacy_root=legacy_root)
    with pytest.raises(RuntimeError, match="archived source"):
        await fixture.run(args)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename", ["rka.db", "embedding_config.json", "synthetic-bge-config.json", fixture.MARKER]
)
async def test_seed_refuses_existing_store_files(tmp_path, no_database_access, filename):
    protected = tmp_path / filename
    protected.write_bytes(b"existing synthetic sentinel")
    args = SimpleNamespace(
        operation="seed", data_dir=tmp_path, legacy_root=Path(rka.__file__).resolve().parents[1]
    )
    with pytest.raises(RuntimeError, match="NEW disposable store"):
        await fixture.run(args)
    assert protected.read_bytes() == b"existing synthetic sentinel"
    assert list(tmp_path.iterdir()) == [protected]


@pytest.mark.asyncio
async def test_seed_refuses_dangling_database_alias(tmp_path, no_database_access):
    target = tmp_path / "elsewhere.db"
    try:
        (tmp_path / "rka.db").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    args = SimpleNamespace(
        operation="seed", data_dir=tmp_path, legacy_root=Path(rka.__file__).resolve().parents[1]
    )
    with pytest.raises(RuntimeError, match="NEW disposable store"):
        await fixture.run(args)
    assert not target.exists()


@pytest.mark.asyncio
async def test_seed_refuses_current_schema_before_creating_db(
    tmp_path, no_database_access, monkeypatch
):
    monkeypatch.setattr(rka, "__version__", "3.0.0")
    args = SimpleNamespace(
        operation="seed", data_dir=tmp_path, legacy_root=Path(rka.__file__).resolve().parents[1]
    )
    with pytest.raises(RuntimeError, match="historical Core version"):
        await fixture.run(args)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("correct_marker", [False, True])
async def test_verify_requires_exact_marker_and_existing_database(
    tmp_path, no_database_access, correct_marker
):
    marker = {"fixture": fixture.FIXTURE if correct_marker else "some-other-fixture"}
    (tmp_path / fixture.MARKER).write_text(json.dumps(marker))
    if not correct_marker:
        (tmp_path / "rka.db").write_bytes(b"existing synthetic sentinel")
    with pytest.raises(RuntimeError, match="exact synthetic fixture marker"):
        await fixture.run(SimpleNamespace(operation="verify", data_dir=tmp_path))
