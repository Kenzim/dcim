from typing import Optional

from sqlalchemy.orm import Session

from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.vmid_reservation_dao import VMIDReservationDAO


DEFAULT_VMID_MIN = 200000
DEFAULT_VMID_MAX = 299999


def _resolve_cluster_range(cluster) -> tuple[int, int]:
    vmid_min = int(cluster.vmid_min) if cluster.vmid_min is not None else DEFAULT_VMID_MIN
    vmid_max = int(cluster.vmid_max) if cluster.vmid_max is not None else DEFAULT_VMID_MAX
    if vmid_min <= 0 or vmid_max <= 0 or vmid_min > vmid_max:
        raise ValueError("Invalid cluster VMID range. Ensure vmid_min and vmid_max are positive and vmid_min <= vmid_max.")
    return vmid_min, vmid_max


def reserve_vmid_for_service(
    db: Session,
    *,
    cluster_id: int,
    service_id: int,
    requested_vmid: Optional[int] = None,
) -> int:
    """
    Reserve a non-reused VMID for a service in the given cluster.
    Reservation rows are intentionally never auto-released.
    """
    existing = VMIDReservationDAO.get_by_service_id(db, service_id)
    if existing:
        if requested_vmid is not None and int(requested_vmid) == int(existing.vmid) and existing.cluster_id == cluster_id:
            return int(existing.vmid)
        if requested_vmid is None and existing.cluster_id == cluster_id:
            return int(existing.vmid)
        if requested_vmid is None and existing.cluster_id != cluster_id:
            raise ValueError(
                f"Service already has reserved VMID {existing.vmid} in cluster {existing.cluster_id}; "
                "cross-cluster reassignment requires an admin override workflow."
            )

    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if cluster is None:
        raise ValueError("Proxmox cluster not found")
    vmid_min, vmid_max = _resolve_cluster_range(cluster)

    if requested_vmid is not None:
        requested = int(requested_vmid)
        if requested < vmid_min or requested > vmid_max:
            raise ValueError(f"Requested VMID {requested} is outside cluster range {vmid_min}-{vmid_max}")
        taken = VMIDReservationDAO.get_by_cluster_vmid(db, cluster_id, requested)
        if taken:
            raise ValueError(f"VMID {requested} is already reserved in cluster {cluster_id}")
        VMIDReservationDAO.create(db, cluster_id=cluster_id, service_id=service_id, vmid=requested)
        return requested

    for vmid in range(vmid_min, vmid_max + 1):
        if VMIDReservationDAO.get_by_cluster_vmid(db, cluster_id, vmid) is None:
            VMIDReservationDAO.create(db, cluster_id=cluster_id, service_id=service_id, vmid=vmid)
            return vmid

    raise ValueError(f"No free VMID left in cluster {cluster_id} range {vmid_min}-{vmid_max}")
