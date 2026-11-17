"""MCP identify_led tool: scopes, argument checks and activity logging."""

from unittest.mock import patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.dao.server_activity_dao import ServerActivityDAO
from tests.api.test_server_identify import _fake_plugin, _server


@pytest.mark.asyncio
async def test_identify_status_is_read_but_setting_needs_write(db_session, mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.power import identify_led

    server = _server(db_session, name="mcp-uid")
    plugin = _fake_plugin()
    with patch("app.services.server_identify.get_registry") as registry:
        registry.return_value.get_plugin.return_value = plugin
        status = await identify_led(server.id)
        assert status["state"] == "off"
        with pytest.raises(ToolError, match="scope"):
            await identify_led(server.id, action="on")
    plugin.set_identify.assert_not_awaited()


@pytest.mark.asyncio
async def test_identify_sets_led_and_logs(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.power import identify_led

    server = _server(db_session, name="mcp-uid-w", locked=True)
    plugin = _fake_plugin()
    with patch("app.services.server_identify.get_registry") as registry:
        registry.return_value.get_plugin.return_value = plugin
        result = await identify_led(server.id, action="on", seconds=60)
        assert result["success"] is True
        plugin.set_identify.assert_awaited_once_with(True, 60)
        with pytest.raises(ToolError, match="seconds only applies"):
            await identify_led(server.id, action="off", seconds=10)
    entries = ServerActivityDAO.get_by_server(db_session, server.id, limit=5)
    assert entries[0].event_type.value == "identify" and entries[0].source == "mcp"
