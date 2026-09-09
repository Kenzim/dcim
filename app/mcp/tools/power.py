"""Server power, boot, and OS reinstall."""

from __future__ import annotations

from typing import Optional

from app.api.server import _server_has_capability
from app.dao.installation_task_dao import InstallationTaskDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.models.server_activity import ServerActivityEventType
from app.models.service import ServiceType
from app.plugins.registry import get_registry
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_failure,
    log_server_activity_success,
)


async def _plugin_for_server(db, server):
    registry = get_registry()
    return registry.get_plugin(server.plugin_name, server.plugin_config)


@mcp.tool()
async def get_server_power_state(server_id: int) -> dict:
    """Read BMC/hypervisor power state for a server."""

    async def work(db, ctx):
        server = ServerDAO.get_by_id(db, server_id)
        if not server:
            raise ValueError("Server not found")
        if not _server_has_capability(db, server, "power_control"):
            raise ValueError("Server does not have power control capability enabled")
        plugin = await _plugin_for_server(db, server)
        state = await plugin.get_power_state()
        return {
            "server_id": server.id,
            "server_name": server.name,
            "power_state": state.value if hasattr(state, "value") else str(state),
        }

    return await run_tool("get_server_power_state", "read", work, args={"server_id": server_id})


@mcp.tool()
async def get_server_boot_options(server_id: int) -> dict:
    """List BMC boot devices and the current boot order."""

    async def work(db, ctx):
        server = ServerDAO.get_by_id(db, server_id)
        if not server:
            raise ValueError("Server not found")
        if not _server_has_capability(db, server, "boot_order"):
            raise ValueError("Boot order capability is disabled for this server")
        plugin = await _plugin_for_server(db, server)
        options = await plugin.get_boot_options()
        try:
            current = await plugin.get_boot_order()
        except Exception as exc:
            current = {"error": str(exc)}
        return {"server_id": server.id, "options": options, "current": current}

    return await run_tool("get_server_boot_options", "read", work, args={"server_id": server_id})


@mcp.tool()
async def list_install_tasks(server_id: int) -> dict:
    """OS installation task history for a server."""

    async def work(db, ctx):
        if not ServerDAO.get_by_id(db, server_id):
            raise ValueError("Server not found")
        tasks = InstallationTaskDAO.get_by_server(db, server_id)
        return {
            "tasks": [
                {
                    "id": t.id,
                    "status": t.status.value if t.status else None,
                    "template_id": t.template_id,
                    "os_name": t.os_name,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "error_message": t.error_message,
                }
                for t in tasks[:50]
            ]
        }

    return await run_tool("list_install_tasks", "read", work, args={"server_id": server_id})


@mcp.tool()
async def server_power(server_id: int, action: str, force: bool = False, confirm: bool = False) -> dict:
    """Power a server: on (write), off/reset (destructive, needs confirm=true)."""
    action_norm = (action or "").strip().lower()
    destructive = action_norm in {"off", "reset", "reboot"}
    min_scope = "destructive" if destructive else "write"

    async def work(db, ctx):
        if action_norm not in {"on", "off", "reset", "reboot"}:
            raise ValueError("action must be on, off, reset, or reboot")
        server = ServerDAO.get_by_id(db, server_id)
        if not server:
            raise ValueError("Server not found")
        if not _server_has_capability(db, server, "power_control"):
            raise ValueError("Server does not have power control capability enabled")
        mapped = "reset" if action_norm == "reboot" else action_norm
        log_server_activity_attempt(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.POWER,
            action=mapped,
            source="mcp",
            message=f"MCP power {mapped} requested",
            details={"mcp_key_id": ctx.key_id, "force": force},
        )
        plugin = await _plugin_for_server(db, server)
        try:
            if mapped == "on":
                success = await plugin.power_on()
            elif mapped == "off":
                success = await plugin.power_off(force=force)
            else:
                success = await plugin.power_reset()
        except Exception as exc:
            log_server_activity_failure(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.POWER,
                action=mapped,
                source="mcp",
                message="MCP power request failed",
                details={"mcp_key_id": ctx.key_id},
                error=exc,
            )
            raise
        logger = log_server_activity_success if success else log_server_activity_failure
        logger(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.POWER,
            action=mapped,
            source="mcp",
            message=f"MCP power {mapped} {'ok' if success else 'failed'}",
            details={"mcp_key_id": ctx.key_id},
        )
        return {"success": bool(success), "server_id": server.id, "action": mapped}

    return await run_tool(
        "server_power",
        min_scope,
        work,
        destructive=destructive,
        confirm=confirm,
        args={"server_id": server_id, "action": action_norm, "force": force, "confirm": confirm},
    )


@mcp.tool()
async def server_set_boot(
    server_id: int,
    device: str,
    persistent: bool = False,
    uefi: Optional[bool] = None,
) -> dict:
    """Set next BMC boot device."""

    async def work(db, ctx):
        server = ServerDAO.get_by_id(db, server_id)
        if not server:
            raise ValueError("Server not found")
        if not _server_has_capability(db, server, "boot_order"):
            raise ValueError("Boot order capability is disabled for this server")
        plugin = await _plugin_for_server(db, server)
        kwargs = {"device": device, "persistent": persistent}
        if uefi is not None:
            kwargs["uefi"] = uefi
        success = await plugin.set_next_boot_device(**kwargs)
        if not success:
            raise ValueError("Failed to set boot device")
        current = await plugin.get_boot_order()
        return {"success": True, "server_id": server.id, "current": current}

    return await run_tool(
        "server_set_boot",
        "write",
        work,
        args={"server_id": server_id, "device": device, "persistent": persistent, "uefi": uefi},
    )


@mcp.tool()
async def server_reinstall_os(
    server_id: int,
    template_id: str,
    template_parameters: Optional[dict] = None,
    confirm: bool = False,
) -> dict:
    """Queue a PXE OS reinstall for the bare-metal service linked to this server."""

    async def work(db, ctx):
        from app.api.billing import _queue_template_install_for_service

        server = ServerDAO.get_by_id(db, server_id)
        if not server:
            raise ValueError("Server not found")
        services = [
            s for s in ServiceDAO.get_by_server(db, server_id) if s.service_type != ServiceType.VM
        ]
        if not services:
            raise ValueError("No bare-metal service linked to this server")
        boot_task, install_task = _queue_template_install_for_service(
            db, services[0], template_id, template_parameters
        )
        return {
            "server_id": server_id,
            "service_id": services[0].id,
            "boot_task_id": boot_task.id,
            "installation_task_id": install_task.id if install_task else None,
            "template_id": template_id,
        }

    return await run_tool(
        "server_reinstall_os",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"server_id": server_id, "template_id": template_id, "confirm": confirm},
    )
