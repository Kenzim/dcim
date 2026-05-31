from __future__ import annotations

from ipaddress import ip_address, IPv4Address
from typing import Optional

from sqlalchemy import and_, update
from sqlalchemy.orm import Session

from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service_vm import ServiceVm
from app.models.vm_ip_allocation import VMIPAllocation


class VMIPAllocationDAO:
    @staticmethod
    def list_all(db: Session) -> list[VMIPAllocation]:
        return db.query(VMIPAllocation).order_by(VMIPAllocation.ip_address.asc()).all()

    @staticmethod
    def get_by_id(db: Session, allocation_id: int) -> Optional[VMIPAllocation]:
        return db.query(VMIPAllocation).filter(VMIPAllocation.id == allocation_id).first()

    @staticmethod
    def get_by_ip(db: Session, ip_value: str) -> Optional[VMIPAllocation]:
        return db.query(VMIPAllocation).filter(VMIPAllocation.ip_address == ip_value).first()

    @staticmethod
    def create(
        db: Session,
        ip_address_value: str,
        subnet_mask: str,
        gateway: str,
        bridge_name: Optional[str],
        cluster_ids: list[int],
        enabled: bool = True,
    ) -> VMIPAllocation:
        row = VMIPAllocation(
            ip_address=ip_address_value,
            subnet_mask=subnet_mask,
            gateway=gateway,
            bridge_name=bridge_name,
            enabled=enabled,
        )
        if cluster_ids:
            clusters = (
                db.query(ProxmoxCluster)
                .filter(ProxmoxCluster.id.in_(cluster_ids))
                .all()
            )
            row.clusters = clusters
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(
        db: Session,
        row: VMIPAllocation,
        subnet_mask: Optional[str] = None,
        gateway: Optional[str] = None,
        bridge_name: Optional[str] = None,
        cluster_ids: Optional[list[int]] = None,
        enabled: Optional[bool] = None,
    ) -> VMIPAllocation:
        if subnet_mask is not None:
            row.subnet_mask = subnet_mask
        if gateway is not None:
            row.gateway = gateway
        if bridge_name is not None:
            row.bridge_name = bridge_name
        if enabled is not None:
            row.enabled = enabled
        if cluster_ids is not None:
            clusters = (
                db.query(ProxmoxCluster)
                .filter(ProxmoxCluster.id.in_(cluster_ids))
                .all()
                if cluster_ids
                else []
            )
            row.clusters = clusters
        db.flush()
        return row

    @staticmethod
    def delete(db: Session, row: VMIPAllocation) -> None:
        db.delete(row)
        db.flush()

    @staticmethod
    def iter_ips_in_range(start_ip: str, end_ip: str) -> list[str]:
        start = ip_address(start_ip)
        end = ip_address(end_ip)
        if not isinstance(start, IPv4Address) or not isinstance(end, IPv4Address):
            raise ValueError("Only IPv4 addresses are supported for bulk add")
        if int(end) < int(start):
            raise ValueError("end_ip must be greater than or equal to start_ip")
        return [str(IPv4Address(value)) for value in range(int(start), int(end) + 1)]

    @staticmethod
    def _allocation_matches_cluster(allocation: VMIPAllocation, proxmox_cluster_id: Optional[int]) -> bool:
        """
        - Pool row with **no** linked clusters: usable for any service (including pending / no placement yet).
        - Pool row linked to specific clusters: only if ``proxmox_cluster_id`` is set and matches one of them.
        """
        linked = list(allocation.clusters or [])
        if not linked:
            return True
        if proxmox_cluster_id is None:
            return False
        return any(c.id == proxmox_cluster_id for c in linked)

    @staticmethod
    def assign_next_free_to_service(
        db: Session,
        *,
        service_id: int,
        proxmox_cluster_id: Optional[int] = None,
    ) -> Optional[VMIPAllocation]:
        """
        Atomically assign the first matching free (enabled, unassigned) VM IP row to ``service_id``.
        Returns None if no row is available. Does not commit — caller should commit with other changes.
        """
        candidates = (
            db.query(VMIPAllocation)
            .filter(
                VMIPAllocation.enabled.is_(True),
                VMIPAllocation.assigned_service_id.is_(None),
            )
            .order_by(VMIPAllocation.ip_address.asc())
            .all()
        )
        for alloc in candidates:
            if not VMIPAllocationDAO._allocation_matches_cluster(alloc, proxmox_cluster_id):
                continue
            result = db.execute(
                update(VMIPAllocation)
                .where(
                    and_(
                        VMIPAllocation.id == alloc.id,
                        VMIPAllocation.assigned_service_id.is_(None),
                    )
                )
                .values(assigned_service_id=service_id)
            )
            if result.rowcount == 1:
                db.flush()
                db.refresh(alloc)
                # Canonical link from the VM extension row (in addition to pool.assigned_service_id).
                db.execute(
                    update(ServiceVm)
                    .where(ServiceVm.service_id == service_id)
                    .values(vm_ip_allocation_id=alloc.id)
                )
                db.flush()
                return alloc
        return None

    @staticmethod
    def release_for_service(db: Session, service_id: int) -> int:
        """Clear pool assignment and ``service_vm.vm_ip_allocation_id`` for this service."""
        db.execute(
            update(ServiceVm)
            .where(ServiceVm.service_id == service_id)
            .values(vm_ip_allocation_id=None)
        )
        result = db.execute(
            update(VMIPAllocation)
            .where(VMIPAllocation.assigned_service_id == service_id)
            .values(assigned_service_id=None)
        )
        return int(result.rowcount or 0)
