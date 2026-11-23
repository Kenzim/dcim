"""Proxmox API tokens as cluster credentials: username ``user@realm!tokenid``, password the secret."""

from types import SimpleNamespace

import httpx
import pytest

from app.plugins.proxmox import ProxmoxPlugin, proxmox_token_header
from app.services import proxmox_vm_search as search

_REAL_CLIENT = httpx.AsyncClient

TOKEN_USER = "rackflow-fw@pve!worker"


def _plugin(username: str) -> ProxmoxPlugin:
    return ProxmoxPlugin({"hostname": "pve.example", "username": username, "password": "s3cret",
                          "node": "pve1", "vmid": 101, "verify_ssl": False})


def test_token_header_only_for_token_ids():
    assert proxmox_token_header(TOKEN_USER, "s3cret") == {"Authorization": f"PVEAPIToken={TOKEN_USER}=s3cret"}
    assert proxmox_token_header("root@pam", "pw") is None


@pytest.mark.asyncio
async def test_plugin_sends_token_header_without_ticket_login(monkeypatch):
    async def no_ticket(self):
        raise AssertionError("an API token must not log in for a ticket")

    monkeypatch.setattr(ProxmoxPlugin, "_get_auth_ticket", no_ticket)
    plugin = _plugin(TOKEN_USER)
    assert await plugin._get_headers() == {"Authorization": f"PVEAPIToken={TOKEN_USER}=s3cret"}


@pytest.mark.asyncio
async def test_plugin_still_uses_ticket_for_password_users(monkeypatch):
    async def ticket(self):
        return {"ticket": "T", "CSRFPreventionToken": "C"}

    monkeypatch.setattr(ProxmoxPlugin, "_get_auth_ticket", ticket)
    headers = await _plugin("root@pam")._get_headers()
    assert headers == {"Cookie": "PVEAuthCookie=T", "CSRFPreventionToken": "C"}


@pytest.mark.asyncio
async def test_plugin_requests_carry_the_token(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": {}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler)))
    plugin = _plugin(TOKEN_USER)
    response = await plugin._get_with_relocate(lambda: f"{plugin.base_url}/api2/json/nodes/pve1/qemu/101/config")
    assert response.status_code == 200
    assert seen == {"auth": f"PVEAPIToken={TOKEN_USER}=s3cret", "path": "/api2/json/nodes/pve1/qemu/101/config"}


@pytest.mark.asyncio
async def test_cluster_search_uses_token_without_ticket(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            raise AssertionError("an API token must not log in for a ticket")

    monkeypatch.setattr(search.httpx, "AsyncClient", Client)
    cluster = SimpleNamespace(id=1, api_url="https://pve/", verify_ssl=False, username=TOKEN_USER, password="s3cret")
    assert await search._cluster_auth_headers(cluster) == {"Authorization": f"PVEAPIToken={TOKEN_USER}=s3cret"}


SECRET = "9f1c2d3e-aaaa-bbbb-cccc-0123456789ab"


def _mock_client(monkeypatch, module, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(module, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=transport))


@pytest.mark.asyncio
async def test_password_login_sends_the_same_requests_and_headers_as_before(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.headers), request.content))
        if request.url.path.endswith("/access/ticket"):
            return httpx.Response(200, json={"data": {"ticket": "T", "CSRFPreventionToken": "C"}})
        return httpx.Response(200, json={"data": {}})

    _mock_client(monkeypatch, httpx, handler)
    plugin = _plugin("root@pam")
    await plugin._get_with_relocate(lambda: f"{plugin.base_url}/api2/json/nodes/pve1/qemu/101/config")
    login, get = seen
    assert login[:2] == ("POST", "/api2/json/access/ticket")
    assert login[3] == b"username=root%40pam&password=s3cret"
    assert "authorization" not in login[2]
    assert get[:2] == ("GET", "/api2/json/nodes/pve1/qemu/101/config")
    assert get[2]["cookie"] == "PVEAuthCookie=T" and get[2]["csrfpreventiontoken"] == "C"
    assert "authorization" not in get[2]


@pytest.mark.asyncio
async def test_password_login_errors_are_unchanged(monkeypatch):
    _mock_client(monkeypatch, httpx, lambda request: httpx.Response(401, json={"data": None}))
    with pytest.raises(Exception, match="Authentication failed: Invalid username or password"):
        await _plugin("root@pam")._get_auth_ticket()
    _mock_client(monkeypatch, httpx, lambda request: httpx.Response(500, json={"data": None}))
    with pytest.raises(Exception, match="HTTP error: 500"):
        await _plugin("root@pam")._get_auth_ticket()
    _mock_client(monkeypatch, httpx, lambda request: httpx.Response(200, json={"data": None}))
    with pytest.raises(Exception, match="No authentication data returned"):
        await _plugin("root@pam")._get_auth_ticket()


@pytest.mark.asyncio
async def test_cluster_search_password_login_is_unchanged(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        return httpx.Response(200, json={"data": {"ticket": "T", "CSRFPreventionToken": "C"}})

    _mock_client(monkeypatch, search.httpx, handler)
    cluster = SimpleNamespace(id=1, api_url="https://pve/", verify_ssl=False, username="root@pam", password="pw")
    assert await search._cluster_auth_headers(cluster) == {"Cookie": "PVEAuthCookie=T", "CSRFPreventionToken": "C"}
    assert seen == [("/api2/json/access/ticket", b"username=root%40pam&password=pw")]


@pytest.mark.asyncio
async def test_a_refused_token_leaves_no_secret_in_errors_or_logs(monkeypatch, caplog):
    import logging

    from app.services.vm_firewall.proxmox_fw import PluginTransport

    caplog.set_level(logging.DEBUG)
    _mock_client(monkeypatch, httpx, lambda request: httpx.Response(
        401, json={"data": None, "errors": {"auth": "invalid token value"}}))
    plugin = ProxmoxPlugin({"hostname": "pve.example", "username": TOKEN_USER, "password": SECRET,
                            "node": "pve1", "vmid": 101, "verify_ssl": False})
    with pytest.raises(Exception) as raised:
        await PluginTransport(plugin).request("GET", "/nodes/{node}/qemu/101/firewall/options")
    assert raised.value.status == 401
    assert SECRET not in str(raised.value) and SECRET not in repr(raised.value)
    assert SECRET not in caplog.text
    assert SECRET not in repr(plugin.__dict__.get("ticket"))


def test_the_proxmox_plugin_config_masks_the_secret():
    from app.services.secret_masking import MASKED_SECRET_PLACEHOLDER, mask_plugin_config

    masked = mask_plugin_config("proxmox", {"hostname": "pve", "username": TOKEN_USER, "password": SECRET})
    assert masked["password"] == MASKED_SECRET_PLACEHOLDER and masked["username"] == TOKEN_USER


@pytest.mark.asyncio
async def test_test_connection_works_with_a_token(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/access/ticket"):
            return httpx.Response(401, json={"data": None})
        return httpx.Response(200, json={"data": {"status": "running", "name": "g0"}})

    _mock_client(monkeypatch, httpx, handler)
    result = await _plugin(TOKEN_USER).test_connection()
    assert result["success"] is True, result
    assert seen == [("/api2/json/nodes/pve1/qemu/101/status/current", f"PVEAPIToken={TOKEN_USER}=s3cret")]


@pytest.mark.asyncio
async def test_test_connection_still_logs_in_for_password_users(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/access/ticket"):
            return httpx.Response(200, json={"data": {"ticket": "T", "CSRFPreventionToken": "C"}})
        return httpx.Response(200, json={"data": {"status": "running"}})

    _mock_client(monkeypatch, httpx, handler)
    plugin = _plugin("root@pam")
    plugin.ticket = "stale"
    assert (await plugin.test_connection())["success"] is True
    assert seen == ["/api2/json/access/ticket", "/api2/json/nodes/pve1/qemu/101/status/current"]
    assert plugin.ticket == "T"
