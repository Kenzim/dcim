"""
Tests for the Proxmox plugin's VNC console helpers (create_vnc_proxy,
vnc_websocket_url, get_vnc_auth_cookie).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plugins.proxmox import ConsoleTypeUnavailable, ProxmoxPlugin


def _make_plugin():
    return ProxmoxPlugin(
        {
            "hostname": "pve.example",
            "username": "root@pam",
            "password": "secret",
            "port": 8006,
            "node": "pve",
            "vmid": 101,
            "verify_ssl": False,
        }
    )


def _fake_httpx_client(json_data):
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = json_data

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_response)
    fake_client.get = AsyncMock(return_value=fake_response)
    return fake_client


@pytest.mark.asyncio
async def test_create_vnc_proxy_returns_port_and_ticket():
    plugin = _make_plugin()
    plugin.ticket = "api-ticket"
    plugin.csrf_token = "csrf-token"

    fake_client = _fake_httpx_client(
        {"data": {"port": 5901, "ticket": "vnc-ticket-abc", "upid": "UPID:1", "cert": "fingerprint"}}
    )

    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        result = await plugin.create_vnc_proxy()

    assert result == {"port": 5901, "ticket": "vnc-ticket-abc", "upid": "UPID:1", "cert": "fingerprint"}
    fake_client.post.assert_called_once()
    args, kwargs = fake_client.post.call_args
    assert args[0] == "https://pve.example:8006/api2/json/nodes/pve/qemu/101/vncproxy"
    assert kwargs["data"] == {"websocket": 1}


@pytest.mark.asyncio
async def test_create_vnc_proxy_raises_without_port_or_ticket():
    plugin = _make_plugin()
    plugin.ticket = "api-ticket"
    plugin.csrf_token = "csrf-token"

    fake_client = _fake_httpx_client({"data": {}})

    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(Exception):
            await plugin.create_vnc_proxy()


def test_vnc_websocket_url_builds_wss_url():
    plugin = _make_plugin()
    url = plugin.vnc_websocket_url(5901, "vnc-ticket-abc")
    assert url.startswith("wss://pve.example:8006/api2/json/nodes/pve/qemu/101/vncwebsocket?")
    assert "port=5901" in url
    assert "vncticket=vnc-ticket-abc" in url


@pytest.mark.asyncio
async def test_get_available_console_types_vnc_only_for_plain_graphical_display():
    plugin = _make_plugin()
    plugin.ticket = "api-ticket"
    plugin.csrf_token = "csrf-token"

    fake_client = _fake_httpx_client({"data": {"vga": "std"}})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        assert await plugin.get_available_console_types() == {"vnc": True, "serial": False}


@pytest.mark.asyncio
async def test_get_available_console_types_vnc_only_when_vga_unset():
    plugin = _make_plugin()
    plugin.ticket = "api-ticket"
    plugin.csrf_token = "csrf-token"

    fake_client = _fake_httpx_client({"data": {}})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        assert await plugin.get_available_console_types() == {"vnc": True, "serial": False}


@pytest.mark.asyncio
async def test_get_available_console_types_serial_only_for_serial_display():
    plugin = _make_plugin()
    plugin.ticket = "api-ticket"
    plugin.csrf_token = "csrf-token"

    fake_client = _fake_httpx_client({"data": {"vga": "serial0", "serial0": "socket"}})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        assert await plugin.get_available_console_types() == {"vnc": False, "serial": True}


@pytest.mark.asyncio
async def test_get_available_console_types_both_when_graphical_with_serial_device():
    """A VM can have a normal graphical display *and* a `serialN: socket`
    device configured for the xterm.js console -- both are then usable and
    the caller (or the end user, via the UI) picks."""
    plugin = _make_plugin()
    plugin.ticket = "api-ticket"
    plugin.csrf_token = "csrf-token"

    fake_client = _fake_httpx_client({"data": {"vga": "std", "serial0": "socket"}})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        assert await plugin.get_available_console_types() == {"vnc": True, "serial": True}


@pytest.mark.asyncio
async def test_get_available_console_types_ignores_non_socket_serial_device():
    plugin = _make_plugin()
    plugin.ticket = "api-ticket"
    plugin.csrf_token = "csrf-token"

    # `serial0: /dev/ttyS0` (passthrough) rather than `socket` -- termproxy
    # can't attach to that.
    fake_client = _fake_httpx_client({"data": {"vga": "std", "serial0": "/dev/ttyS0"}})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        assert await plugin.get_available_console_types() == {"vnc": True, "serial": False}


@pytest.mark.asyncio
async def test_create_term_proxy_returns_port_and_ticket():
    plugin = _make_plugin()
    plugin.ticket = "api-ticket"
    plugin.csrf_token = "csrf-token"

    fake_client = _fake_httpx_client(
        {"data": {"port": 5902, "ticket": "term-ticket-abc", "upid": "UPID:2", "user": "root@pam"}}
    )
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        result = await plugin.create_term_proxy()

    assert result == {"port": 5902, "ticket": "term-ticket-abc", "upid": "UPID:2", "user": "root@pam"}
    args, kwargs = fake_client.post.call_args
    assert args[0] == "https://pve.example:8006/api2/json/nodes/pve/qemu/101/termproxy"


@pytest.mark.asyncio
async def test_create_term_proxy_raises_without_port_or_ticket():
    plugin = _make_plugin()
    plugin.ticket = "api-ticket"
    plugin.csrf_token = "csrf-token"

    fake_client = _fake_httpx_client({"data": {}})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(Exception):
            await plugin.create_term_proxy()


@pytest.mark.asyncio
async def test_open_console_proxy_defaults_to_vnc_when_available(monkeypatch):
    plugin = _make_plugin()
    monkeypatch.setattr(
        plugin, "get_available_console_types", AsyncMock(return_value={"vnc": True, "serial": True})
    )
    monkeypatch.setattr(plugin, "create_vnc_proxy", AsyncMock(return_value={"port": 5901, "ticket": "vnc-tix"}))
    monkeypatch.setattr(plugin, "create_term_proxy", AsyncMock(side_effect=AssertionError("should not be called")))

    result = await plugin.open_console_proxy()
    assert result == {
        "port": 5901,
        "ticket": "vnc-tix",
        "console_type": "vnc",
        "available_console_types": {"vnc": True, "serial": True},
    }


@pytest.mark.asyncio
async def test_open_console_proxy_falls_back_to_serial_when_vnc_unavailable(monkeypatch):
    plugin = _make_plugin()
    monkeypatch.setattr(
        plugin, "get_available_console_types", AsyncMock(return_value={"vnc": False, "serial": True})
    )
    monkeypatch.setattr(plugin, "create_term_proxy", AsyncMock(return_value={"port": 5902, "ticket": "term-tix"}))
    monkeypatch.setattr(plugin, "create_vnc_proxy", AsyncMock(side_effect=AssertionError("should not be called")))

    result = await plugin.open_console_proxy()
    assert result["console_type"] == "serial"


@pytest.mark.asyncio
async def test_open_console_proxy_honors_explicit_choice(monkeypatch):
    plugin = _make_plugin()
    monkeypatch.setattr(
        plugin, "get_available_console_types", AsyncMock(return_value={"vnc": True, "serial": True})
    )
    monkeypatch.setattr(plugin, "create_term_proxy", AsyncMock(return_value={"port": 5902, "ticket": "term-tix"}))
    monkeypatch.setattr(plugin, "create_vnc_proxy", AsyncMock(side_effect=AssertionError("should not be called")))

    result = await plugin.open_console_proxy(console_type="serial")
    assert result["console_type"] == "serial"


@pytest.mark.asyncio
async def test_open_console_proxy_rejects_unavailable_explicit_choice(monkeypatch):
    plugin = _make_plugin()
    monkeypatch.setattr(
        plugin, "get_available_console_types", AsyncMock(return_value={"vnc": True, "serial": False})
    )

    with pytest.raises(ConsoleTypeUnavailable):
        await plugin.open_console_proxy(console_type="serial")


@pytest.mark.asyncio
async def test_get_vnc_auth_cookie_uses_api_auth_ticket(monkeypatch):
    plugin = _make_plugin()

    async def fake_get_headers():
        plugin.ticket = "api-cookie-ticket"
        plugin.csrf_token = "csrf"
        return {"Cookie": f"PVEAuthCookie={plugin.ticket}", "CSRFPreventionToken": plugin.csrf_token}

    monkeypatch.setattr(plugin, "_get_headers", fake_get_headers)

    cookie = await plugin.get_vnc_auth_cookie()
    assert cookie == "PVEAuthCookie=api-cookie-ticket"
