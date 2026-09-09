"""Compact JSON projections for MCP tools (no secrets)."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.location import Location
from app.models.rack import Rack
from app.models.server import Server
from app.models.service import Service
from app.models.user import User
from app.services.service_resource import service_server_id_for_response, vm_placement


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def location_row(row: Location) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "description": row.description}


def rack_row(row: Rack) -> dict[str, Any]:
    return {
        "id": row.id,
        "location_id": row.location_id,
        "name": row.name,
        "description": row.description,
        "units": row.units,
        "row": row.row,
        "row_position": row.row_position,
    }


def server_row(row: Server, *, caps: Optional[list] = None) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "name": row.name,
        "server_ip": row.server_ip,
        "description": row.description,
        "location_id": row.location_id,
        "rack_id": row.rack_id,
        "rack_unit": row.rack_unit,
        "rack_units": row.rack_units,
        "plugin_name": row.plugin_name,
        "enabled": row.enabled,
        "cpu_count": row.cpu_count,
        "cpu_model": row.cpu_model,
        "ram_gb": row.ram_gb,
        "ipmi_proxy_enabled": bool(row.ipmi_proxy_enabled),
    }
    if caps is not None:
        payload["capabilities"] = caps
    return payload


def service_row(db: Session, row: Service) -> dict[str, Any]:
    cid, node, vmid = vm_placement(row)
    owner = row.owner_user
    return {
        "id": row.id,
        "name": row.name,
        "service_type": row.service_type.value if row.service_type else None,
        "status": row.status.value if row.status else None,
        "owner_user_id": row.owner_user_id,
        "owner_username": owner.username if owner else None,
        "server_id": service_server_id_for_response(row),
        "product_code": row.product_code,
        "os_code": row.os_code,
        "proxmox_cluster_id": cid,
        "proxmox_node_name": node,
        "proxmox_vmid": vmid,
        "vm_guest_state": row.vm.guest_state.value if row.vm and row.vm.guest_state else None,
        "created_at": _iso(row.created_at),
        "terminated_at": _iso(row.terminated_at),
    }


def client_row(row: User) -> dict[str, Any]:
    return {
        "id": row.id,
        "username": row.username,
        "email": row.email,
        "has_password": bool(row.has_password),
        "permission_set_id": row.permission_set_id,
        "billing_integration_id": row.billing_integration_id,
    }


def not_found(kind: str, ident) -> dict[str, Any]:
    return {"error": "not_found", "type": kind, "id": ident}
