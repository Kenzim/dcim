import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import dhcp_service as mod


class Client:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.post, self.get = AsyncMock(), AsyncMock()
        for method in (self.post, self.get):
            method.side_effect = error or (lambda *a, **k: response)
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


def response(data, status=200):
    r = MagicMock(status_code=status)
    r.json.return_value = data
    return r


@pytest.mark.parametrize("separate,legacy,expected", [
    ("http://runner/", "", ("http://runner", "")),
    ("", "http://combined/", ("http://combined", "/dhcp")),
    ("", "", ("", "")),
])
def test_remote_base(monkeypatch, separate, legacy, expected):
    monkeypatch.setattr(mod.settings, "dhcp_runner_url", separate)
    monkeypatch.setattr(mod.settings, "dhcp_tftp_service_url", legacy)
    assert mod._remote_base() == expected
    assert mod._has_runner() is bool(separate or legacy)


@pytest.mark.parametrize("method", ["start", "stop", "restart", "reload"])
@pytest.mark.asyncio
async def test_lifecycle_without_runner(monkeypatch, method):
    monkeypatch.setattr(mod.settings, "dhcp_runner_url", "")
    monkeypatch.setattr(mod.settings, "dhcp_tftp_service_url", "")
    result = await getattr(mod.DHCPService(), method)()
    assert result["success"] is False
    assert result["running"] is False


@pytest.mark.parametrize("method,path,status", [
    ("start", "/start", mod.DHCPStatus.RUNNING),
    ("stop", "/stop", mod.DHCPStatus.STOPPED),
    ("reload", "/reload", mod.DHCPStatus.RUNNING),
])
@pytest.mark.asyncio
async def test_lifecycle_posts_to_runner(monkeypatch, method, path, status):
    monkeypatch.setattr(mod.settings, "dhcp_runner_url", "http://runner/")
    monkeypatch.setattr(mod.settings, "dhcp_tftp_service_url", "")
    client = Client(response({"success": True, "status": "ok", "message": "done", "pid": 9}))
    with patch("httpx.AsyncClient", return_value=client):
        svc = mod.DHCPService()
        result = await getattr(svc, method)()
    client.post.assert_awaited_once_with("http://runner" + path)
    assert result["success"] and svc.status == status


@pytest.mark.parametrize("method", ["start", "stop", "reload"])
@pytest.mark.asyncio
async def test_lifecycle_runner_error(monkeypatch, method):
    monkeypatch.setattr(mod.settings, "dhcp_runner_url", "http://runner")
    client = Client(error=RuntimeError("offline"))
    with patch("httpx.AsyncClient", return_value=client):
        result = await getattr(mod.DHCPService(), method)()
    assert result["success"] is False and "offline" in result["message"]


@pytest.mark.asyncio
async def test_restart_stops_sleeps_then_starts(monkeypatch):
    monkeypatch.setattr(mod.settings, "dhcp_runner_url", "http://runner")
    svc = mod.DHCPService()
    svc.stop, svc.start = AsyncMock(return_value={"success": True}), AsyncMock(return_value={"success": True})
    with patch.object(mod.asyncio, "sleep", new=AsyncMock()) as sleep:
        assert await svc.restart() == {"success": True}
    svc.stop.assert_awaited_once(); svc.start.assert_awaited_once(); sleep.assert_awaited_once_with(1)


@pytest.mark.parametrize("running,expected", [(True, "running"), (False, "stopped")])
@pytest.mark.asyncio
async def test_get_status_uses_remote_data(monkeypatch, running, expected):
    monkeypatch.setattr(mod.settings, "dhcp_runner_url", "http://runner")
    client = Client(response({"running": running, "pid": 3, "config_path": "/x", "lease_path": "/l"}))
    with patch("httpx.AsyncClient", return_value=client):
        result = await mod.DHCPService().get_status()
    client.get.assert_awaited_once_with("http://runner/status")
    assert result["status"] == expected and result["pid"] == 3


@pytest.mark.asyncio
async def test_status_error_and_cleanup(monkeypatch):
    monkeypatch.setattr(mod.settings, "dhcp_runner_url", "http://runner")
    with patch("httpx.AsyncClient", return_value=Client(error=RuntimeError("bad"))):
        svc = mod.DHCPService(config_path="/c", lease_file="/l")
        assert (await svc.get_status())["status"] == "error"
    assert await svc.cleanup() is None


def test_load_config_from_service_and_singleton(monkeypatch):
    cfg = MagicMock(config_file_path="/db.conf", lease_file_path="/db.lease",
                    interfaces=[MagicMock(interface="ens9")])
    db = MagicMock()
    service = mod.DHCPService(); service._db = db
    with patch("app.services.dhcp_config_service.get_dhcp_config_service") as get:
        get.return_value.get_config.return_value = cfg
        service._load_config_from_service()
    assert str(service.config_path) == "/db.conf" and service.interface == "ens9"
    monkeypatch.setattr(mod, "_dhcp_service", None)
    assert mod.get_dhcp_service(db) is mod.get_dhcp_service()
