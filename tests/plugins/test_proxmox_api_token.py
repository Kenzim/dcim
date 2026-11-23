"""Proxmox API tokens as cluster credentials: username ``user@realm!tokenid``, password the secret."""

from types import SimpleNamespace

import httpx
import pytest

from app.plugins.proxmox import ProxmoxPlugin, proxmox_token_header
from app.services import proxmox_vm_search as search

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

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport))
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
