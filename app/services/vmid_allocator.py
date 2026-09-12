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


def _existing_sticky_reservation(
    existing,
    *,
    cluster_id: int,
    service_id: int,
    requested_vmid: Optional[int],
) -> Optional[int]:
    if not existing:
        return None
    if (
        requested_vmid is not None
        and int(requested_vmid) == int(existing.vmid)
        and existing.cluster_id == cluster_id
    ):
        return int(existing.vmid)
    if requested_vmid is None and existing.cluster_id == cluster_id:
        return int(existing.vmid)
    if requested_vmid is None and existing.cluster_id != cluster_id:
        raise ValueError(
            f"Service already has reserved VMID {existing.vmid} in cluster {existing.cluster_id}; "
            "cross-cluster reassignment requires an admin override workflow."
        )
    return None


def _reserve_explicit_vmid(
    db: Session,
    *,
    cluster_id: int,
    service_id: int,
    requested_vmid: int,
    vmid_min: int,
    vmid_max: int,
    allow_outside_range: bool,
) -> int:
    requested = int(requested_vmid)
    if not allow_outside_range and (requested < vmid_min or requested > vmid_max):
        raise ValueError(f"Requested VMID {requested} is outside cluster range {vmid_min}-{vmid_max}")
    if requested <= 0:
        raise ValueError("Requested VMID must be positive")
    taken = VMIDReservationDAO.get_by_cluster_vmid(db, cluster_id, requested)
    if taken:
        raise ValueError(f"VMID {requested} is already reserved in cluster {cluster_id}")
    VMIDReservationDAO.create(db, cluster_id=cluster_id, service_id=service_id, vmid=requested)
    return requested


def _reserve_next_free_vmid(
    db: Session,
    *,
    cluster_id: int,
    service_id: int,
    vmid_min: int,
    vmid_max: int,
    suggested_vmid: Optional[int],
) -> int:
    if suggested_vmid is not None:
        suggested = int(suggested_vmid)
        if (
            vmid_min <= suggested <= vmid_max
            and VMIDReservationDAO.get_by_cluster_vmid(db, cluster_id, suggested) is None
        ):
            VMIDReservationDAO.create(db, cluster_id=cluster_id, service_id=service_id, vmid=suggested)
            return suggested
    for vmid in range(vmid_min, vmid_max + 1):
        if VMIDReservationDAO.get_by_cluster_vmid(db, cluster_id, vmid) is None:
            VMIDReservationDAO.create(db, cluster_id=cluster_id, service_id=service_id, vmid=vmid)
            return vmid
    raise ValueError(f"No free VMID left in cluster {cluster_id} range {vmid_min}-{vmid_max}")


def reserve_vmid_for_service(
    db: Session,
    *,
    cluster_id: int,
    service_id: int,
    requested_vmid: Optional[int] = None,
    suggested_vmid: Optional[int] = None,
    allow_outside_range: bool = False,
) -> int:
    """
    Reserve a non-reused VMID for a service in the given cluster.
    Reservation rows are intentionally never auto-released.

    ``suggested_vmid`` (e.g. from Proxmox ``/cluster/nextid``) is tried first
    when auto-allocating, so new IDs stay aligned with unique-next-id / used_vmids.
    Sticky re-provision must pass the service's existing reservation (or omit
    both args) and must not treat used_vmids membership as a hard reject.

    ``allow_outside_range`` is for adopting an existing guest whose VMID sits
    outside the cluster's auto-allocation window.
    """
    existing = VMIDReservationDAO.get_by_service_id(db, service_id)
    sticky = _existing_sticky_reservation(
        existing, cluster_id=cluster_id, service_id=service_id, requested_vmid=requested_vmid
    )
    if sticky is not None:
        return sticky

    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if cluster is None:
        raise ValueError("Proxmox cluster not found")
    vmid_min, vmid_max = _resolve_cluster_range(cluster)

    if requested_vmid is not None:
        return _reserve_explicit_vmid(
            db,
            cluster_id=cluster_id,
            service_id=service_id,
            requested_vmid=int(requested_vmid),
            vmid_min=vmid_min,
            vmid_max=vmid_max,
            allow_outside_range=allow_outside_range,
        )

    return _reserve_next_free_vmid(
        db,
        cluster_id=cluster_id,
        service_id=service_id,
        vmid_min=vmid_min,
        vmid_max=vmid_max,
        suggested_vmid=suggested_vmid,
    )


async def _validate_requested_vmid_on_proxmox(
    plugin,
    *,
    requested_vmid: int,
    node_name: str,
    adopt_existing: bool,
) -> None:
    if not adopt_existing:
        ok = await plugin.check_vmid_available_for_new(int(requested_vmid))
        if not ok:
            raise ValueError(
                f"VMID {requested_vmid} is not available for new allocation on Proxmox "
                "(may already be marked non-reusable)"
            )
        return
    if not await plugin.vm_exists():
        raise ValueError(f"No Proxmox guest with VMID {requested_vmid} on node {node_name}")


async def reserve_vmid_aligned_with_proxmox(
    db: Session,
    *,
    cluster_id: int,
    service_id: int,
    node_name: str,
    requested_vmid: Optional[int] = None,
    adopt_existing: bool = False,
) -> int:
    """
    Reserve a VMID, consulting Proxmox nextid for *new* allocations.

    Existing sticky reservations are returned unchanged (no nextid / used_vmids reject).
    When ``adopt_existing`` is true, bind a VMID that already exists on Proxmox
    (skip nextid availability; allow outside auto-allocation range).
    """
    existing = VMIDReservationDAO.get_by_service_id(db, service_id)
    if existing and existing.cluster_id == cluster_id and (
        requested_vmid is None or int(requested_vmid) == int(existing.vmid)
    ):
        return int(existing.vmid)

    from app.plugins.registry import get_registry
    from app.services.proxmox_placement import cluster_to_proxmox_plugin_config

    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if cluster is None:
        raise ValueError("Proxmox cluster not found")

    placeholder_vmid = int(requested_vmid) if requested_vmid is not None else 0
    plugin_config = cluster_to_proxmox_plugin_config(cluster, str(node_name).strip(), placeholder_vmid or 1)
    plugin = get_registry().get_plugin("proxmox", plugin_config)

    if requested_vmid is not None:
        await _validate_requested_vmid_on_proxmox(
            plugin,
            requested_vmid=int(requested_vmid),
            node_name=node_name,
            adopt_existing=adopt_existing,
        )
        return reserve_vmid_for_service(
            db,
            cluster_id=cluster_id,
            service_id=service_id,
            requested_vmid=int(requested_vmid),
            allow_outside_range=bool(adopt_existing),
        )

    suggested = None
    try:
        suggested = await plugin.get_next_vmid()
    except Exception:
        suggested = None
    return reserve_vmid_for_service(
        db,
        cluster_id=cluster_id,
        service_id=service_id,
        suggested_vmid=suggested,
    )
