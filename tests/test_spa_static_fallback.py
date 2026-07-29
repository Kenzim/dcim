"""Unit tests for the SPA static-file fallback path-containment check in app/main.py.

Calls the route handler directly (bypassing HTTP/ASGI routing) so the
underlying containment logic can be exercised with crafted ``full_path``
values regardless of how any particular web server normalizes URLs.
"""
import asyncio

import pytest
from fastapi import HTTPException

import app.main as main_module


def test_spa_fallback_serves_existing_static_file(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "app.js").write_text("console.log('hi');")

    monkeypatch.setattr(main_module, "static_path", static_dir)

    response = asyncio.run(main_module.serve_spa("app.js"))
    assert str(response.path) == str(static_dir / "app.js")


def test_spa_fallback_blocks_traversal_to_sibling_directory(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>ok</html>")

    # Sibling directory whose name merely shares a string prefix with
    # static_dir -- a naive str(path).startswith(str(static_dir)) check
    # would incorrectly treat files under this directory as "contained"
    # within static_dir, once escaped there via "..".
    sibling_dir = tmp_path / "static-evil"
    sibling_dir.mkdir()
    (sibling_dir / "secret.txt").write_text("top secret")

    monkeypatch.setattr(main_module, "static_path", static_dir)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main_module.serve_spa("../static-evil/secret.txt"))
    assert exc_info.value.status_code == 404


def test_spa_fallback_blocks_traversal_outside_static_dir(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>ok</html>")

    outside_secret = tmp_path / "secret.txt"
    outside_secret.write_text("top secret")

    monkeypatch.setattr(main_module, "static_path", static_dir)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main_module.serve_spa("../secret.txt"))
    assert exc_info.value.status_code == 404
