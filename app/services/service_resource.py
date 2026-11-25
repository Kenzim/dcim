"""Resolve Server / Proxmox placement from polymorphic Service rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

from sqlalchemy.orm import Session

from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import Service, ServiceStatus, ServiceType

if TYPE_CHECKING:
    from app.models.server import Server


def is_current_occupant(db: Session, service: Service, server: Optional["Server"]) -> bool:
    """True when ``service`` is live and is the only live service on ``server``."""
    if server is None or service.status == ServiceStatus.TERMINATED:
        return False
    return not any(
        other.id != service.id and other.status != ServiceStatus.TERMINATED
        for other in ServiceDAO.get_by_server(db, server.id)
    )


def service_linked_server(
    db: Session, service: Service, *, include_history: bool = False
) -> Optional["Server"]:
    """Bare-metal / http_proxy: the rack Server the service currently holds. VM: None.

    A terminated service, or one that shares its server with another live
    service, no longer holds the machine, so it resolves to None and nothing
    can be done to the server's current occupant through it. ``include_history``
    returns the stored link regardless, for termination, clean-up and staff
    history views only.
    """
    if service.service_type == ServiceType.VM:
        return None
    bm = service.bare_metal
    if not bm:
        return None
    server = ServerDAO.get_by_id(db, bm.server_id)
    if include_history or is_current_occupant(db, service, server):
        return server
    return None


def server_unavailable_reason(db: Session, service: Service) -> Optional[str]:
    """Why ``service`` may not act on its server, or None when it may (or has none)."""
    if service.service_type == ServiceType.VM or not getattr(service, "bare_metal", None):
        return None
    if service.status == ServiceStatus.TERMINATED:
        return "Service is terminated"
    if service_linked_server(db, service) is None and service_linked_server(
        db, service, include_history=True
    ) is not None:
        return "Service no longer holds its server"
    return None


def service_server_id_for_response(service: Service) -> Optional[int]:
    bm = service.bare_metal
    return bm.server_id if bm else None


def vm_placement(service: Service) -> Tuple[Optional[int], Optional[str], Optional[int]]:
    """Return (cluster_id, node_name, vmid) from service_vm extension, or Nones."""
    if service.service_type != ServiceType.VM or not service.vm:
        return (None, None, None)
    v = service.vm
    return (v.proxmox_cluster_id, v.proxmox_node_name, v.proxmox_vmid)
