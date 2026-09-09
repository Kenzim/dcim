"""Location-scoped DHCP/TFTP status, logs, and controls."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.api.location_dhcp import _get_dhcp_instance
from app.api.location_tftp import _get_tftp_instance
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.services.dhcp_config_service import get_dhcp_config_service
from app.services.runner_client import call_dhcp_runner, call_tftp_runner


def _dump(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


@mcp.tool()
async def dhcp_tftp_status(location_id: int, kind: str = "dhcp") -> dict:
    """DHCP or TFTP runner status for a location."""

    async def work(db, ctx):
        kind_norm = (kind or "dhcp").strip().lower()
        if kind_norm == "dhcp":
            instance = _get_dhcp_instance(db, location_id)
            code, body = await call_dhcp_runner(instance, db, "GET", "/status")
        elif kind_norm == "tftp":
            instance = _get_tftp_instance(db, location_id)
            code, body = await call_tftp_runner(instance, db, "GET", "/status")
        else:
            raise ValueError("kind must be dhcp or tftp")
        if code != 200:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
        return {"location_id": location_id, "kind": kind_norm, "status": body}

    return await run_tool(
        "dhcp_tftp_status", "read", work, args={"location_id": location_id, "kind": kind}
    )


@mcp.tool()
async def dhcp_tftp_settings(location_id: int, kind: str = "dhcp") -> dict:
    """DHCP settings or TFTP runner config for a location."""

    async def work(db, ctx):
        kind_norm = (kind or "dhcp").strip().lower()
        if kind_norm == "dhcp":
            instance = _get_dhcp_instance(db, location_id)
            config_service = get_dhcp_config_service()
            cfg = config_service.get_config_by_service_instance(db, instance.id)
            if cfg is None:
                cfg = config_service.get_or_create_config_for_service_instance(
                    db, instance.id, "/shared/dhcp/dhcpd.conf", "/shared/dhcp/dhcpd.leases"
                )
            return {"location_id": location_id, "kind": "dhcp", "settings": _dump(cfg)}
        if kind_norm == "tftp":
            instance = _get_tftp_instance(db, location_id)
            code, body = await call_tftp_runner(instance, db, "GET", "/config")
            if code != 200:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
            return {"location_id": location_id, "kind": "tftp", "settings": body}
        raise ValueError("kind must be dhcp or tftp")

    return await run_tool(
        "dhcp_tftp_settings", "read", work, args={"location_id": location_id, "kind": kind}
    )


@mcp.tool()
async def dhcp_tftp_logs(location_id: int, kind: str = "dhcp", limit: int = 100) -> dict:
    """Recent DHCP or TFTP runner logs."""

    async def work(db, ctx):
        kind_norm = (kind or "dhcp").strip().lower()
        cap = max(1, min(int(limit or 100), 500))
        if kind_norm == "dhcp":
            instance = _get_dhcp_instance(db, location_id)
            code, body = await call_dhcp_runner(instance, db, "GET", f"/logs?limit={cap}")
        elif kind_norm == "tftp":
            instance = _get_tftp_instance(db, location_id)
            code, body = await call_tftp_runner(instance, db, "GET", f"/logs?limit={cap}")
        else:
            raise ValueError("kind must be dhcp or tftp")
        if code != 200:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
        return {"location_id": location_id, "kind": kind_norm, "logs": body}

    return await run_tool(
        "dhcp_tftp_logs",
        "read",
        work,
        args={"location_id": location_id, "kind": kind, "limit": limit},
    )


@mcp.tool()
async def dhcp_tftp_control(
    location_id: int, kind: str, action: str, confirm: bool = False
) -> dict:
    """Start/stop/restart a DHCP or TFTP runner, or regen DHCP config. Destructive."""

    async def work(db, ctx):
        kind_norm = (kind or "").strip().lower()
        action_norm = (action or "").strip().lower()
        if kind_norm not in {"dhcp", "tftp"}:
            raise ValueError("kind must be dhcp or tftp")
        allowed = {"start", "stop", "restart"}
        if kind_norm == "dhcp":
            allowed = allowed | {"regen"}
        if action_norm not in allowed:
            raise ValueError(f"action must be one of {sorted(allowed)}")
        if action_norm == "regen":
            from app.services.dhcp_config_generator import generate_dhcpd_conf

            instance = _get_dhcp_instance(db, location_id)
            config_svc = get_dhcp_config_service()
            config = config_svc.get_config_by_service_instance(db, instance.id)
            if not config:
                config = config_svc.get_or_create_config_for_service_instance(
                    db, instance.id, "/shared/dhcp/dhcpd.conf", "/shared/dhcp/dhcpd.leases"
                )
            result = generate_dhcpd_conf(config, db, location_id=location_id, return_content=True)
            if not result:
                raise ValueError("Failed to generate DHCP config")
            content, interface_names = result
            headers = {"X-Runner-Interfaces": ",".join(interface_names)} if interface_names else None
            code, body = await call_dhcp_runner(
                instance, db, "PUT", "/config", raw_body=content.encode(), extra_headers=headers
            )
            if code not in (200, 201):
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
            return {"success": True, "kind": "dhcp", "action": "regen"}

        instance = (
            _get_dhcp_instance(db, location_id)
            if kind_norm == "dhcp"
            else _get_tftp_instance(db, location_id)
        )
        caller = call_dhcp_runner if kind_norm == "dhcp" else call_tftp_runner
        code, body = await caller(instance, db, "POST", f"/{action_norm}")
        if code not in (200, 201):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
        return {"success": True, "kind": kind_norm, "action": action_norm, "result": body}

    return await run_tool(
        "dhcp_tftp_control",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"location_id": location_id, "kind": kind, "action": action, "confirm": confirm},
    )
