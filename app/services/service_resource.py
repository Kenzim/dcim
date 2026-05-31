"""Resolve Server / Proxmox placement from polymorphic Service rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

from sqlalchemy.orm import Session

from app.dao.server_dao import ServerDAO
from app.models.service import Service, ServiceType

if TYPE_CHECKING:
    from app.models.server import Server


def service_linked_server(db: Session, service: Service) -> Optional["Server"]:
    """Bare-metal / http_proxy: linked rack Server. VM: None."""
    if service.service_type == ServiceType.VM:
        return None
    bm = service.bare_metal
    if not bm:
        return None
    return ServerDAO.get_by_id(db, bm.server_id)


def service_server_id_for_response(service: Service) -> Optional[int]:
    bm = service.bare_metal
    return bm.server_id if bm else None


def vm_placement(service: Service) -> Tuple[Optional[int], Optional[str], Optional[int]]:
    """Return (cluster_id, node_name, vmid) from service_vm extension, or Nones."""
    if service.service_type != ServiceType.VM or not service.vm:
        return (None, None, None)
    v = service.vm
    return (v.proxmox_cluster_id, v.proxmox_node_name, v.proxmox_vmid)
