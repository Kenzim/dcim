import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services import runner_client as mod


class Client:
    def __init__(self, response=None, error=None):
        self.get, self.post, self.put = AsyncMock(), AsyncMock(), AsyncMock()
        for method in (self.get, self.post, self.put):
            method.side_effect = error or (lambda *a, **k: response)
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


def instance():
    return MagicMock(base_url="http://runner/")


def response(json_value={"ok": True}, text="plain", code=200):
    r = MagicMock(status_code=code, text=text); r.json.return_value = json_value
    return r


@pytest.mark.parametrize("key,expected", [(None, {}), ("", {}), ("secret", {"X-API-Key": "secret"})])
def test_headers(key, expected):
    assert mod._headers(key) == expected


@pytest.mark.parametrize("func,method", [
    (mod.call_dhcp_runner, "GET"), (mod.call_dhcp_runner, "POST"), (mod.call_dhcp_runner, "PUT"),
    (mod.call_tftp_runner, "GET"), (mod.call_tftp_runner, "POST"), (mod.call_tftp_runner, "PUT"),
])
@pytest.mark.asyncio
async def test_runner_calls_json(monkeypatch, func, method):
    client = Client(response(code=201))
    monkeypatch.setattr(mod.ServiceInstanceDAO, "get_api_key", lambda _: "key")
    with patch("httpx.AsyncClient", return_value=client):
        code, body = await func(instance(), MagicMock(), method, "/status", json_body={"x": 1})
    assert (code, body) == (201, {"ok": True})
    assert getattr(client, method.lower()).await_count == 1


@pytest.mark.parametrize("func", [mod.call_dhcp_runner, mod.call_tftp_runner])
@pytest.mark.asyncio
async def test_runner_returns_text_when_json_invalid(monkeypatch, func):
    r = response(text="not-json"); r.json.side_effect = ValueError("bad")
    monkeypatch.setattr(mod.ServiceInstanceDAO, "get_api_key", lambda _: None)
    with patch("httpx.AsyncClient", return_value=Client(r)):
        assert await func(instance(), MagicMock(), "GET", "/x") == (200, "not-json")


@pytest.mark.parametrize("func", [mod.call_dhcp_runner, mod.call_tftp_runner])
@pytest.mark.asyncio
async def test_runner_rejects_unsupported_method(func):
    assert await func(instance(), MagicMock(), "DELETE", "/x") == (405, {"detail": "Method not allowed"})


@pytest.mark.parametrize("func", [mod.call_dhcp_runner, mod.call_tftp_runner])
@pytest.mark.asyncio
async def test_runner_connection_error(monkeypatch, func):
    monkeypatch.setattr(mod.ServiceInstanceDAO, "get_api_key", lambda _: "key")
    with patch("httpx.AsyncClient", return_value=Client(error=RuntimeError("offline"))):
        code, body = await func(instance(), MagicMock(), "GET", "/x")
    assert code == 0 and body["detail"] == "offline"


@pytest.mark.asyncio
async def test_dhcp_extra_headers_and_raw_body(monkeypatch):
    client = Client(response())
    monkeypatch.setattr(mod.ServiceInstanceDAO, "get_api_key", lambda _: "key")
    with patch("httpx.AsyncClient", return_value=client):
        await mod.call_dhcp_runner(instance(), None, "POST", "/upload", raw_body=b"x",
                                   extra_headers={"Content-Type": "text/plain"})
    client.post.assert_awaited_once_with(
        "http://runner/upload", headers={"X-API-Key": "key", "Content-Type": "text/plain"},
        json={}, content=b"x"
    )
