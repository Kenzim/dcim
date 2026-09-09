"""SPA catch-all skips /mcp so the feature-flagged mount (or 404) wins."""

import asyncio

from fastapi import HTTPException

import app.main as main_module


def test_spa_fallback_404s_mcp_prefix():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_module.serve_spa("mcp"))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc2:
        asyncio.run(main_module.serve_spa("mcp/sse"))
    assert exc2.value.status_code == 404


def test_mcp_http_404_when_disabled(client, mock_redis):
    assert client.get("/mcp").status_code == 404
    assert client.post("/mcp", json={}).status_code == 404
    assert client.post("/mcp/", json={}).status_code == 404
