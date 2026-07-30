import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock, patch
from app.api import dhcp, tftp


@pytest.mark.asyncio
@pytest.mark.parametrize("func", [dhcp.stop_dhcp_server, dhcp.restart_dhcp_server, dhcp.reload_dhcp_server,
                                   tftp.start_tftp_server, tftp.stop_tftp_server, tftp.restart_tftp_server, tftp.reload_tftp_server])
async def test_lifecycle_handlers_return_success(func):
    service = MagicMock()
    getattr(service, func.__name__.split("_")[0] if False else "start", AsyncMock())
    method = {"stop_dhcp_server":"stop","restart_dhcp_server":"restart","reload_dhcp_server":"reload",
              "start_tftp_server":"start","stop_tftp_server":"stop","restart_tftp_server":"restart","reload_tftp_server":"reload"}[func.__name__]
    setattr(service, method, AsyncMock(return_value={"success": True, "message": "ok"}))
    kwargs = {"dhcp_service" if "dhcp" in func.__name__ else "tftp_service": service}
    if "dhcp" in func.__name__ and method in {"restart", "reload"}:
        cfg = MagicMock(); kwargs.update(db=MagicMock(), config_service=MagicMock(get_config=MagicMock(return_value=cfg)))
        with patch.object(dhcp, "generate_dhcpd_conf"):
            assert (await func(**kwargs))["success"]
    else:
        assert (await func(**kwargs))["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("func,service_kw", [
    (dhcp.stop_dhcp_server, "dhcp_service"), (tftp.stop_tftp_server, "tftp_service"),
    (tftp.start_tftp_server, "tftp_service"), (tftp.reload_tftp_server, "tftp_service"),
])
async def test_lifecycle_handlers_raise_on_failure(func, service_kw):
    service = MagicMock()
    method = func.__name__.split("_")[0]
    setattr(service, method, AsyncMock(return_value={"success": False, "message": "failed"}))
    with pytest.raises(HTTPException) as exc:
        await func(**{service_kw: service})
    assert exc.value.status_code == 500 and exc.value.detail == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("func,kw", [(dhcp.get_dhcp_status, "dhcp_service"), (tftp.get_tftp_status, "tftp_service")])
async def test_status_handlers(func, kw):
    service = MagicMock(get_status=AsyncMock(return_value={"running": True}))
    assert await func(**{kw: service}) == {"running": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("func,kw", [(dhcp.get_dhcp_config, "config_service"), (tftp.get_tftp_config, "config_service")])
async def test_config_get_handlers(func, kw):
    db, service = MagicMock(), MagicMock(get_config=MagicMock(return_value="config"))
    assert await func(db=db, **{kw: service}) == "config"


@pytest.mark.asyncio
async def test_dhcp_start_generates_config_and_reports_generation_failure():
    db, cfg = MagicMock(), MagicMock()
    config_service, service = MagicMock(get_config=MagicMock(return_value=cfg)), MagicMock(start=AsyncMock(return_value={"success": True}))
    with patch.object(dhcp, "generate_dhcpd_conf") as generate:
        assert (await dhcp.start_dhcp_server(db=db, config_service=config_service, dhcp_service=service))["success"]
    generate.assert_called_once_with(cfg, db)
    with patch.object(dhcp, "generate_dhcpd_conf", side_effect=RuntimeError("write")):
        with pytest.raises(HTTPException, match="generate"):
            await dhcp.start_dhcp_server(db=db, config_service=config_service, dhcp_service=service)


@pytest.mark.asyncio
@pytest.mark.parametrize("running,enabled,expected", [(True, True, "restart"), (False, True, "start"), (False, False, None)])
async def test_tftp_update_reconciles_runner(running, enabled, expected):
    config = MagicMock(enabled=enabled)
    config_service = MagicMock(update_config=MagicMock(return_value=config))
    service = MagicMock(get_status=AsyncMock(return_value={"running": running}), start=AsyncMock(), restart=AsyncMock())
    data = tftp.TFTPConfigUpdate(enabled=enabled, bind_port=1069)
    assert await tftp.update_tftp_config(data, db=MagicMock(), config_service=config_service, tftp_service=service) is config
    if expected: getattr(service, expected).assert_awaited_once()


@pytest.mark.asyncio
async def test_dhcp_regenerate_reloads_only_running():
    cfg, db = MagicMock(), MagicMock()
    config_service = MagicMock(get_config=MagicMock(return_value=cfg))
    service = MagicMock(get_status=AsyncMock(return_value={"running": True}), reload=AsyncMock())
    with patch.object(dhcp, "generate_dhcpd_conf"):
        result = await dhcp.regenerate_dhcp_config(db=db, config_service=config_service, dhcp_service=service)
    assert result["status"] == "regenerated"; service.reload.assert_awaited_once()
