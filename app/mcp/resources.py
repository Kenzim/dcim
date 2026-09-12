"""Read-only MCP resources for large gets."""

from __future__ import annotations

import json

from mcp.server.fastmcp.exceptions import ToolError

from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.mcp.serialize import location_row, server_row, service_row
from app.api.server import _get_effective_capabilities_for_server

_INVALID_ID_MSG = "id must be an integer"


@mcp.resource("rackflow://server/{id}")
async def server_resource(id: str) -> str:
    """Full compact server record including effective capabilities."""

    def work(db, ctx):
        try:
            sid = int(id)
        except (TypeError, ValueError) as exc:
            raise ToolError(_INVALID_ID_MSG) from exc
        row = ServerDAO.get_by_id(db, sid)
        if not row:
            raise ToolError("Server not found")
        caps = _get_effective_capabilities_for_server(db, row)
        return server_row(row, caps=caps)

    payload = await run_tool("resource_server", "read", work, args={"server_id": id})
    return json.dumps(payload)


@mcp.resource("rackflow://service/{id}")
async def service_resource(id: str) -> str:
    """Full compact service record (no passwords or guest credentials)."""

    def work(db, ctx):
        try:
            sid = int(id)
        except (TypeError, ValueError) as exc:
            raise ToolError(_INVALID_ID_MSG) from exc
        row = ServiceDAO.get_by_id(db, sid)
        if not row:
            raise ToolError("Service not found")
        return service_row(db, row)

    payload = await run_tool("resource_service", "read", work, args={"service_id": id})
    return json.dumps(payload)


@mcp.resource("rackflow://location/{id}")
async def location_resource(id: str) -> str:
    """Location record."""

    def work(db, ctx):
        try:
            lid = int(id)
        except (TypeError, ValueError) as exc:
            raise ToolError(_INVALID_ID_MSG) from exc
        row = LocationDAO.get_by_id(db, lid)
        if not row:
            raise ToolError("Location not found")
        return location_row(row)

    payload = await run_tool("resource_location", "read", work, args={"location_id": id})
    return json.dumps(payload)
