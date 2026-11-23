"""A password (ticket) cluster: Test connection and the console cookie log in for a ticket as they always have."""

import httpx
import pytest

from app.plugins.proxmox import ProxmoxPlugin

_REAL_CLIENT = httpx.AsyncClient


def _plugin() -> ProxmoxPlugin:
    return ProxmoxPlugin({"hostname": "pve.example", "username": "root@pam", "password": "pw",
                          "node": "pve1", "vmid": 101, "verify_ssl": False})


def _serve(monkeypatch, seen, ticket_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content, dict(request.headers)))
        if request.url.path.endswith("/access/ticket"):
            if ticket_status != 200:
                return httpx.Response(ticket_status, json={"data": None})
            return httpx.Response(200, json={"data": {"ticket": "T1", "CSRFPreventionToken": "C1"}})
        return httpx.Response(200, json={"data": {"status": "running", "name": "g0"}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_test_connection_logs_in_afresh_then_reads_the_status(monkeypatch):
    seen = []
    _serve(monkeypatch, seen)
    plugin = _plugin()
    plugin.ticket = "stale"
    result = await plugin.test_connection()
    assert result["success"] is True
    assert [(m, p, c) for m, p, c, _ in seen] == [
        ("POST", "/api2/json/access/ticket", b"username=root%40pam&password=pw"),
        ("GET", "/api2/json/nodes/pve1/qemu/101/status/current", b""),
    ]
    status_headers = seen[1][3]
    assert status_headers["cookie"] == "PVEAuthCookie=T1" and "authorization" not in status_headers
    assert plugin.ticket == "T1" and plugin.csrf_token == "C1"


@pytest.mark.asyncio
async def test_test_connection_reports_a_refused_login(monkeypatch):
    _serve(monkeypatch, [], ticket_status=401)
    result = await _plugin().test_connection()
    assert result["success"] is False
    assert "Invalid username or password" in result["message"]


@pytest.mark.asyncio
async def test_console_cookie_is_the_ticket_cookie(monkeypatch):
    seen = []
    _serve(monkeypatch, seen)
    plugin = _plugin()
    assert await plugin.get_vnc_auth_cookie() == "PVEAuthCookie=T1"
    assert [p for _, p, _, _ in seen] == ["/api2/json/access/ticket"]
    assert await plugin.get_vnc_auth_cookie() == "PVEAuthCookie=T1"
    assert len(seen) == 1
