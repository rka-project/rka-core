"""Regression checks for the independently installable RKA Core profile."""

from __future__ import annotations

import tomllib
from importlib.util import find_spec
from pathlib import Path

from rka.config import RKAConfig
from tests.ownership import AGENTIC_TEST_PATHS, WRITER_TEST_PATHS, owner_for_test


ROOT = Path(__file__).resolve().parents[1]


def _project_metadata() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _requirements(extra: str) -> set[str]:
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    values = payload["project"]["optional-dependencies"][extra]
    return {value.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0] for value in values}


def test_core_embeddings_are_separate_from_legacy_llm_providers() -> None:
    embeddings = _requirements("embeddings")
    llm = _requirements("llm")

    assert embeddings == {"fastembed", "sqlite-vec"}
    assert {"litellm", "instructor"} <= llm
    assert embeddings.isdisjoint(llm)


def test_core_defaults_to_no_server_side_llm(monkeypatch) -> None:
    monkeypatch.delenv("RKA_LLM_ENABLED", raising=False)
    assert RKAConfig(_env_file=None).llm_enabled is False


def test_core_distribution_keeps_the_stable_import_and_cli_names() -> None:
    metadata = _project_metadata()
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert metadata["name"] == "rka-core"
    assert metadata["scripts"]["rka"] == "rka.cli:main"
    assert find_spec("rka.__main__") is not None
    locked_core = next(p for p in tomllib.loads(lock)["package"] if p["name"] == "rka-core")
    assert locked_core["version"] == metadata["version"]
    assert locked_core["source"] == {"editable": "."}


def test_base_distribution_defaults_to_no_optional_embeddings(monkeypatch) -> None:
    monkeypatch.delenv("RKA_EMBEDDINGS_ENABLED", raising=False)
    assert RKAConfig(_env_file=None).embeddings_enabled is False


def test_docker_profile_explicitly_selects_data_and_embeddings() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "RKA_DATA_DIR=/data" in dockerfile
    assert "RKA_DB_PATH=/data/rka.db" in dockerfile
    assert "RKA_EMBEDDINGS_ENABLED=true" in dockerfile
    assert 'RKA_EMBEDDINGS_ENABLED: "true"' in compose


def test_downstream_test_manifest_is_valid_and_disjoint() -> None:
    assert WRITER_TEST_PATHS
    assert AGENTIC_TEST_PATHS
    assert WRITER_TEST_PATHS.isdisjoint(AGENTIC_TEST_PATHS)

    declared = WRITER_TEST_PATHS | AGENTIC_TEST_PATHS
    missing = sorted(path for path in declared if not (ROOT / path).is_file())
    assert not missing, f"ownership manifest contains missing tests: {missing}"

    assert all(owner_for_test(path) == "writer" for path in WRITER_TEST_PATHS)
    assert all(owner_for_test(path) == "agentic" for path in AGENTIC_TEST_PATHS)
    assert owner_for_test("tests/test_core_profile.py") == "core"
