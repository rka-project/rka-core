"""Offline/runtime resource policy parity; no model or provider calls."""

import json
from pathlib import Path

import pytest

from rka.infra.embedding_backends import make_backend
from rka.infra.embedding_resources import embedding_resource_limits
from rka.services.embedding_config import EmbeddingConfigService
from rka.services.embedding_inspection import EmbeddingInspectionError, _read_config
from tests.test_cli.test_embedding_inspect import invoke, setup_store


@pytest.mark.parametrize("backend", ["openai_compat", "ollama", "fastembed"])
@pytest.mark.parametrize("size", [8192, 8193, 16384, 16385])
def test_offline_limits_match_runtime(tmp_path, backend, size):
    sub = {"model": "synthetic", "model_name": "synthetic", "dim": 768,
           "base_url": "http://never-contact.invalid",
           "resource_limits": {"max_input_bytes": size}}
    config = {"backend": backend, "config": sub}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    ceiling = 8192 if backend == "fastembed" else 16384
    if size > ceiling:
        with pytest.raises(ValueError):
            make_backend(config)
        with pytest.raises(EmbeddingInspectionError, match="^resource_limits_invalid$"):
            _read_config(path)
    else:
        # Backend constructors validate budgets but must not run inference.
        runtime = make_backend(config)
        assert runtime.resource_limits == embedding_resource_limits(backend, sub["resource_limits"])
        assert _read_config(path)[1]["backend"] == backend
        if backend == "fastembed":
            assert runtime.resource_limits.max_input_bytes == 2048
            assert runtime.resource_limits.max_padding_bytes == 4096


@pytest.mark.parametrize("backend", ["openai_compat", "ollama"])
@pytest.mark.parametrize("limits", [None, {}, {"max_input_bytes": 4096},
                                   {"max_padding_bytes": 4096}])
def test_partial_and_legacy_http_budgets_stay_compatible(tmp_path, backend, limits):
    config = {"backend": backend, "config": {
        "model": "synthetic", "dim": 768, "base_url": "http://unused.invalid",
        "resource_limits": limits,
    }}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    actual = make_backend(config).resource_limits
    assert actual == embedding_resource_limits(backend, limits)
    assert actual.max_input_bytes == (4096 if limits else 16384)
    _read_config(path)


@pytest.mark.parametrize("limits", [
    {"max_input_bytes": True}, {"max_input_bytes": 0},
    {"max_input_bytes": "secret-value"}, {"secret-field": "secret-value"},
    {"max_input_bytes": 16384, "max_batch_bytes": 8192}, "secret-value",
])
def test_invalid_resource_error_is_specific_and_does_not_leak(tmp_path, limits):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"backend": "openai_compat", "config": {
        "model": "synthetic", "dim": 768, "base_url": "http://unused.invalid",
        "api_key": "secret-api-key", "resource_limits": limits,
    }}))
    with pytest.raises(EmbeddingInspectionError) as error:
        _read_config(path)
    assert str(error.value) == "resource_limits_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["inspect", "dry-run"])
async def test_real_inspection_accepts_current_and_candidate_http_16k(db, monkeypatch, command):
    config = await setup_store(db)
    config.config["resource_limits"] = {"max_input_bytes": 16384}
    folder = Path(db.db_path).parent
    EmbeddingConfigService(folder).save_config(config, "system")
    candidate = folder / "candidate.json"
    candidate.write_text(config.model_dump_json())

    def forbidden(*args, **kwargs):
        raise AssertionError("offline inspection must not construct a provider")

    monkeypatch.setattr("rka.infra.embedding_backends.make_backend", forbidden)
    args = ("--target-config", str(candidate)) if command == "dry-run" else ()
    result = await invoke(db, command, *args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["totals"]["hash_verified"] == 7
    assert "synthetic-private-token" not in result.output
    assert not await db.fetchone("SELECT id FROM jobs")
