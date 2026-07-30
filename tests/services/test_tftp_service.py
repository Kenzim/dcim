import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services import tftp_service as mod


class Client:
    def __init__(self, response=None, error=None):
        self.post, self.get = AsyncMock(), AsyncMock()
        for call in (self.post, self.get):
            call.side_effect = error or (lambda *a, **k: response)
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


def result(data):
    response = MagicMock(); response.json.return_value = data
    return response


@pytest.mark.parametrize("separate,legacy,expected", [
    ("http://tftp/", "", ("http://tftp", "")),
    ("", "http://both/", ("http://both", "/tftp")),
    ("", "", ("", "")),
])
def test_remote_base_and_runner(monkeypatch, separate, legacy, expected):
    monkeypatch.setattr(mod.settings, "tftp_runner_url", separate)
    monkeypatch.setattr(mod.settings, "dhcp_tftp_service_url", legacy)
    assert mod._remote_base() == expected
    assert mod._has_runner() == bool(separate or legacy)


@pytest.mark.parametrize("method", ["start", "stop", "restart", "reload"])
@pytest.mark.asyncio
async def test_lifecycle_requires_runner(monkeypatch, method):
    monkeypatch.setattr(mod.settings, "tftp_runner_url", "")
    monkeypatch.setattr(mod.settings, "dhcp_tftp_service_url", "")
    assert not (await getattr(mod.TFTPService(), method)())["success"]


@pytest.mark.parametrize("method,path,state", [
    ("start", "/start", mod.TFTPStatus.RUNNING),
    ("stop", "/stop", mod.TFTPStatus.STOPPED),
    ("reload", "/restart", mod.TFTPStatus.RUNNING),
])
@pytest.mark.asyncio
async def test_lifecycle_posts_expected_endpoint(monkeypatch, method, path, state):
    monkeypatch.setattr(mod.settings, "tftp_runner_url", "http://runner/")
    client = Client(result({"success": True, "status": "ok", "message": "ok", "pid": 4}))
    with patch("httpx.AsyncClient", return_value=client):
        svc = mod.TFTPService()
        assert (await getattr(svc, method)())["success"]
    client.post.assert_awaited_once_with("http://runner" + path)
    assert svc.status == state


@pytest.mark.parametrize("method", ["start", "stop", "reload"])
@pytest.mark.asyncio
async def test_lifecycle_failure_is_reported(monkeypatch, method):
    monkeypatch.setattr(mod.settings, "tftp_runner_url", "http://runner")
    with patch("httpx.AsyncClient", return_value=Client(error=OSError("down"))):
        outcome = await getattr(mod.TFTPService(), method)()
    assert outcome["success"] is False and "down" in outcome["message"]


@pytest.mark.asyncio
async def test_restart_sequence(monkeypatch):
    monkeypatch.setattr(mod.settings, "tftp_runner_url", "http://runner")
    svc = mod.TFTPService()
    svc.stop, svc.start = AsyncMock(return_value={"success": True}), AsyncMock(return_value={"success": True})
    with patch.object(mod.asyncio, "sleep", new=AsyncMock()) as sleep:
        await svc.restart()
    svc.stop.assert_awaited_once(); svc.start.assert_awaited_once(); sleep.assert_awaited_once_with(1)


@pytest.mark.parametrize("running,expected", [(True, "running"), (False, "stopped")])
@pytest.mark.asyncio
async def test_status_success(monkeypatch, running, expected):
    monkeypatch.setattr(mod.settings, "tftp_runner_url", "http://runner")
    client = Client(result({"running": running, "pid": 6, "root_directory": "/srv"}))
    with patch("httpx.AsyncClient", return_value=client):
        status = await mod.TFTPService().get_status()
    assert status["status"] == expected and status["root_directory"] == "/srv"
    client.get.assert_awaited_once_with("http://runner/status")


@pytest.mark.asyncio
async def test_status_failure_cleanup_load_and_singleton(monkeypatch):
    monkeypatch.setattr(mod.settings, "tftp_runner_url", "http://runner")
    svc = mod.TFTPService(root_directory="/old"); svc._db = MagicMock()
    cfg = MagicMock(root_directory="/new", bind_address="127.0.0.1", bind_port=1069)
    with patch("app.services.tftp_config_service.get_tftp_config_service") as getter:
        getter.return_value.get_config.return_value = cfg
        svc._load_config_from_service()
    assert str(svc.root_directory) == "/new"
    with patch("httpx.AsyncClient", return_value=Client(error=OSError("bad"))):
        assert (await svc.get_status())["status"] == "error"
    assert await svc.cleanup() is None
    monkeypatch.setattr(mod, "_tftp_service", None)
    assert mod.get_tftp_service(MagicMock()) is mod.get_tftp_service()
