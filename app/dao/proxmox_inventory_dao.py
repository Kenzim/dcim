from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.proxmox_inventory import (
    ProxmoxCluster,
    ProxmoxNode,
    ProxmoxStorage,
    ProxmoxTemplate,
    ProxmoxCapacitySnapshot,
)


class ProxmoxInventoryDAO:
    @staticmethod
    def create_cluster(
        db: Session,
        name: str,
        api_url: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
        vmid_min: Optional[int] = None,
        vmid_max: Optional[int] = None,
        details: Optional[dict] = None,
    ) -> ProxmoxCluster:
        row = ProxmoxCluster(
            name=name,
            api_url=api_url,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
            vmid_min=vmid_min,
            vmid_max=vmid_max,
            details=details or {},
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_cluster(db: Session, cluster_id: int) -> Optional[ProxmoxCluster]:
        return db.query(ProxmoxCluster).filter(ProxmoxCluster.id == cluster_id).first()

    @staticmethod
    def update_cluster(db: Session, row: ProxmoxCluster, **kwargs) -> ProxmoxCluster:
        for key, value in kwargs.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def list_clusters(db: Session) -> List[ProxmoxCluster]:
        return db.query(ProxmoxCluster).order_by(ProxmoxCluster.name).all()

    @staticmethod
    def upsert_node(db: Session, cluster_id: int, node_name: str, details: Optional[dict] = None) -> ProxmoxNode:
        row = (
            db.query(ProxmoxNode)
            .filter(ProxmoxNode.cluster_id == cluster_id, ProxmoxNode.node_name == node_name)
            .first()
        )
        if row:
            row.details = details or row.details
            db.commit()
            db.refresh(row)
            return row
        row = ProxmoxNode(cluster_id=cluster_id, node_name=node_name, details=details or {})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def upsert_storage(
        db: Session,
        node_id: int,
        storage_name: str,
        storage_type: Optional[str] = None,
        total_bytes: Optional[int] = None,
        used_bytes: Optional[int] = None,
        details: Optional[dict] = None,
    ) -> ProxmoxStorage:
        row = (
            db.query(ProxmoxStorage)
            .filter(ProxmoxStorage.node_id == node_id, ProxmoxStorage.storage_name == storage_name)
            .first()
        )
        if not row:
            row = ProxmoxStorage(node_id=node_id, storage_name=storage_name)
            db.add(row)
        row.storage_type = storage_type
        row.total_bytes = total_bytes
        row.used_bytes = used_bytes
        row.details = details or {}
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def upsert_template(
        db: Session,
        node_id: int,
        vmid: int,
        name: str,
        storage_name: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> ProxmoxTemplate:
        row = (
            db.query(ProxmoxTemplate)
            .filter(ProxmoxTemplate.node_id == node_id, ProxmoxTemplate.vmid == vmid)
            .first()
        )
        if not row:
            row = ProxmoxTemplate(node_id=node_id, vmid=vmid, name=name)
            db.add(row)
        row.name = name
        row.storage_name = storage_name
        row.details = details or {}
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def find_template_vmid_on_node(
        db: Session,
        *,
        cluster_id: int,
        node_name: str,
        template_name: str,
    ) -> Optional[int]:
        """
        Resolve a synced ``ProxmoxTemplate.name`` (matches catalog ``proxmox_template_name``) to QEMU vmid.
        """
        node = (
            db.query(ProxmoxNode)
            .filter(ProxmoxNode.cluster_id == cluster_id, ProxmoxNode.node_name == node_name)
            .first()
        )
        if not node:
            return None
        row = (
            db.query(ProxmoxTemplate)
            .filter(ProxmoxTemplate.node_id == node.id, ProxmoxTemplate.name == template_name)
            .first()
        )
        return row.vmid if row else None

    @staticmethod
    def add_capacity_snapshot(
        db: Session,
        node_id: int,
        cpu_total: Optional[float] = None,
        cpu_used: Optional[float] = None,
        ram_total_bytes: Optional[int] = None,
        ram_used_bytes: Optional[int] = None,
        storage_total_bytes: Optional[int] = None,
        storage_used_bytes: Optional[int] = None,
        overcommit_ratio: Optional[float] = None,
    ) -> ProxmoxCapacitySnapshot:
        row = ProxmoxCapacitySnapshot(
            node_id=node_id,
            cpu_total=cpu_total,
            cpu_used=cpu_used,
            ram_total_bytes=ram_total_bytes,
            ram_used_bytes=ram_used_bytes,
            storage_total_bytes=storage_total_bytes,
            storage_used_bytes=storage_used_bytes,
            overcommit_ratio=overcommit_ratio,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
