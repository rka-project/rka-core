"""The SPA fallback must not expose files outside its configured static root."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka.config import RKAConfig


@pytest_asyncio.fixture
async def static_client(tmp_path, monkeypatch):
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<title>test app</title>", encoding="utf-8")
    (dist / "logo.svg").write_text("<svg>test logo</svg>", encoding="utf-8")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("/* test asset */", encoding="utf-8")
    (tmp_path / "sentinel.txt").write_text("PRIVATE-SYNTHETIC-SENTINEL", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    app = create_app(
        RKAConfig(
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            db_path=tmp_path / "unused.db",
            embeddings_enabled=False,
            llm_enabled=False,
        )
    )
    # Static tests need no database, server socket, or lifespan/provider startup.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, dist, tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/%2e%2e/sentinel.txt",
        "/..%2f..%2fsentinel.txt",
        "/%2e%2e/%2e%2e/missing.txt",
    ],
)
async def test_encoded_traversal_is_rejected(static_client, path):
    client, _, _ = static_client
    response = await client.get(path)
    assert response.status_code == 404
    assert "PRIVATE-SYNTHETIC-SENTINEL" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["linked.txt", "assets/linked.txt", "parent/linked.txt"])
async def test_outside_symlink_is_not_served(static_client, location):
    client, dist, root = static_client
    try:
        if location.startswith("parent/"):
            (dist / "parent").symlink_to(root, target_is_directory=True)
            path = "/parent/sentinel.txt"
        else:
            (dist / location).symlink_to(root / "sentinel.txt")
            path = f"/{location}"
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this test host: {exc}")
    response = await client.get(path)
    assert response.status_code == 404
    assert "PRIVATE-SYNTHETIC-SENTINEL" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, expected",
    [
        ("/", "<title>test app</title>"),
        ("/projects/fixture/notes", "<title>test app</title>"),
        ("/logo.svg", "<svg>test logo</svg>"),
        ("/assets/app.js", "/* test asset */"),
    ],
)
async def test_normal_assets_and_spa_routes_still_work(static_client, path, expected):
    client, _, _ = static_client
    response = await client.get(path)
    assert response.status_code == 200
    assert response.text == expected


@pytest.mark.asyncio
async def test_invalid_filesystem_path_returns_controlled_not_found(static_client):
    client, _, _ = static_client
    response = await client.get("/bad%00path")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_fallback_cannot_serve_an_outside_index_symlink(static_client):
    client, dist, root = static_client
    (dist / "index.html").unlink()
    try:
        (dist / "index.html").symlink_to(root / "sentinel.txt")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this test host: {exc}")
    for path in ("/", "/projects/fixture/notes", "/index.html"):
        response = await client.get(path)
        assert response.status_code == 404
        assert "PRIVATE-SYNTHETIC-SENTINEL" not in response.text


@pytest.mark.asyncio
async def test_symlink_loop_returns_controlled_not_found(static_client):
    client, dist, _ = static_client
    try:
        (dist / "loop").symlink_to(dist / "loop")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this test host: {exc}")
    assert (await client.get("/loop")).status_code == 404


@pytest.mark.asyncio
async def test_static_root_does_not_follow_cwd_changes(static_client, monkeypatch):
    client, dist, _ = static_client
    monkeypatch.chdir(dist)
    assert (await client.get("/logo.svg")).text == "<svg>test logo</svg>"


@pytest.mark.asyncio
async def test_outside_assets_root_is_not_mounted(tmp_path, monkeypatch):
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<title>test app</title>", encoding="utf-8")
    (tmp_path / "sentinel.txt").write_text("PRIVATE-SYNTHETIC-SENTINEL", encoding="utf-8")
    try:
        (dist / "assets").symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this test host: {exc}")
    monkeypatch.chdir(tmp_path)
    app = create_app(
        RKAConfig(
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            db_path=tmp_path / "unused.db",
            embeddings_enabled=False,
            llm_enabled=False,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/assets/sentinel.txt")
    assert response.status_code == 404
    assert "PRIVATE-SYNTHETIC-SENTINEL" not in response.text
