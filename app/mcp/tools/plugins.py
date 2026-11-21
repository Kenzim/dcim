"""Staff MCP access to plugins through one fixed tool, so tools/list never grows with plugins."""

from __future__ import annotations

from typing import Literal, Optional

from mcp.server.fastmcp.exceptions import ToolError

from app.mcp.instance import mcp
from app.mcp.runtime import run_tool


@mcp.tool()
async def plugins(
    action: Literal["list", "tools", "call", "panel"] = "list",
    plugin_id: Optional[str] = None,
    tool: Optional[str] = None,
    params: Optional[dict] = None,
    service_id: Optional[int] = None,
    confirm: bool = False,
) -> dict:
    """Installed plugins. list; tools (plugin_id): its staff tools; call (plugin_id, tool, params, confirm);
    panel (service_id): plugin panels of a service."""
    from app.plugin_host import lifecycle, ui
    from app.models.plugin_host import PluginInstallation
    from app.models.service import Service
    from sqlalchemy import select

    args = {"action": action, "plugin_id": plugin_id, "tool": tool, "service_id": service_id}

    def need(**values) -> None:
        missing = [k for k, v in values.items() if v in (None, "")]
        if missing:
            raise ToolError(f"{action} needs {', '.join(missing)}")

    def read(db, ctx):
        if action == "list":
            rows = db.execute(select(PluginInstallation).order_by(PluginInstallation.id)).scalars()
            items = []
            for inst in rows:
                summary = lifecycle.summary(db, inst)
                items.append({k: summary[k] for k in ("id", "name", "version", "status", "health")})
            return {"items": items}
        if action == "tools":
            need(plugin_id=plugin_id)
            try:
                return {"items": ui.staff_tools(db, plugin_id)}
            except ui.PluginRequestError as exc:
                raise ToolError(exc.detail) from exc
        need(service_id=service_id)
        service = db.get(Service, service_id)
        if service is None:
            raise ToolError("Service not found")
        return ui.render(db, service, "admin")

    if action != "call":
        return await run_tool("plugins", "read", read, args=args)

    async def call(db, ctx):
        need(plugin_id=plugin_id, tool=tool)
        try:
            inst = db.get(PluginInstallation, plugin_id)
            spec = None
            if inst is not None:
                from app.plugin_host import runtime

                spec = runtime.manifest_of(inst).mcp_tool(tool)
            scope = spec.scope if spec is not None else "read"
            from app.mcp.scopes import scope_allows

            if not scope_allows(ctx.scopes, scope):
                raise ToolError(f"MCP key lacks required scope: {scope}")
            if scope == "destructive" and not confirm:
                raise ToolError("This action is destructive; pass confirm=true to proceed")
            actor = {"type": "staff", "user_id": ctx.created_by_user_id, "via": "mcp"}
            result = await ui.run_tool(db, plugin_id, tool, params=params, actor=actor, audience="staff")
        except ui.PluginRequestError as exc:
            raise ToolError(exc.detail) from exc
        return {"result": result}

    return await run_tool("plugins", "read", call, args=args)
