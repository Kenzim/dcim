from typing import Any, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.proxmox_inventory import (
    ProxmoxCluster,
    ProxmoxNode,
    ProxmoxStorage,
    ProxmoxTemplate,
    ProxmoxCapacitySnapshot,
)

# Snapshots are inserted on every manual/scheduled sync and never updated in
# place, so left unbounded they grow forever. Keeping this many recent rows
# per node is enough for placement scoring / trend display while capping
# table growth; older rows are pruned right after each sync.
CAPACITY_SNAPSHOT_RETENTION_PER_NODE = 100


class ProxmoxInventoryDAO:
    @staticmethod
    def create_cluster(
        db: Session,
        name: str,
        api_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
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
    def get_cluster_capacity_summary(db: Session) -> List[dict[str, Any]]:
        """
        Lightweight per-cluster summary for list views: node/template/storage
        counts and the most recent sync timestamp, computed with a handful of
        aggregate queries instead of hydrating every node's full templates /
        storages / capacity_snapshots collections (the latter grows unbounded
        across syncs, so loading it in full for every list request does not
        scale — see ``CAPACITY_SNAPSHOT_RETENTION_PER_NODE`` pruning below).
        """
        clusters = ProxmoxInventoryDAO.list_clusters(db)
        if not clusters:
            return []
        cluster_ids = [c.id for c in clusters]

        node_counts = dict(
            db.query(ProxmoxNode.cluster_id, func.count(ProxmoxNode.id))
            .filter(ProxmoxNode.cluster_id.in_(cluster_ids))
            .group_by(ProxmoxNode.cluster_id)
            .all()
        )
        template_counts = dict(
            db.query(ProxmoxNode.cluster_id, func.count(ProxmoxTemplate.id))
            .join(ProxmoxTemplate, ProxmoxTemplate.node_id == ProxmoxNode.id)
            .filter(ProxmoxNode.cluster_id.in_(cluster_ids))
            .group_by(ProxmoxNode.cluster_id)
            .all()
        )
        storage_counts = dict(
            db.query(ProxmoxNode.cluster_id, func.count(ProxmoxStorage.id))
            .join(ProxmoxStorage, ProxmoxStorage.node_id == ProxmoxNode.id)
            .filter(ProxmoxNode.cluster_id.in_(cluster_ids))
            .group_by(ProxmoxNode.cluster_id)
            .all()
        )
        last_synced_at = dict(
            db.query(ProxmoxNode.cluster_id, func.max(ProxmoxCapacitySnapshot.created_at))
            .join(ProxmoxCapacitySnapshot, ProxmoxCapacitySnapshot.node_id == ProxmoxNode.id)
            .filter(ProxmoxNode.cluster_id.in_(cluster_ids))
            .group_by(ProxmoxNode.cluster_id)
            .all()
        )

        result = []
        for cluster in clusters:
            last_sync = last_synced_at.get(cluster.id)
            result.append(
                {
                    "cluster_id": cluster.id,
                    "cluster_name": cluster.name,
                    # Aliases for admin UIs that expect id/name (same as list endpoints elsewhere)
                    "id": cluster.id,
                    "name": cluster.name,
                    "api_url": cluster.api_url,
                    "enabled": cluster.enabled,
                    "vmid_min": cluster.vmid_min,
                    "vmid_max": cluster.vmid_max,
                    "node_count": node_counts.get(cluster.id, 0),
                    "template_count": template_counts.get(cluster.id, 0),
                    "storage_count": storage_counts.get(cluster.id, 0),
                    "last_synced_at": last_sync.isoformat() if last_sync else None,
                }
            )
        return result

    @staticmethod
    def prune_capacity_snapshots(
        db: Session, node_id: int, keep: int = CAPACITY_SNAPSHOT_RETENTION_PER_NODE
    ) -> int:
        """Delete all but the ``keep`` most recent snapshots for a node. Caller commits."""
        keep_ids = [
            row[0]
            for row in (
                db.query(ProxmoxCapacitySnapshot.id)
                .filter(ProxmoxCapacitySnapshot.node_id == node_id)
                # id as a tiebreaker: created_at has only second-level resolution
                # on some backends, so bursts of inserts within the same second
                # would otherwise sort arbitrarily.
                .order_by(ProxmoxCapacitySnapshot.created_at.desc(), ProxmoxCapacitySnapshot.id.desc())
                .limit(keep)
                .all()
            )
        ]
        query = db.query(ProxmoxCapacitySnapshot).filter(ProxmoxCapacitySnapshot.node_id == node_id)
        if keep_ids:
            query = query.filter(~ProxmoxCapacitySnapshot.id.in_(keep_ids))
        return query.delete(synchronize_session=False)

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
    def list_distinct_storage_names(
        db: Session,
        *,
        backup_capable_only: bool = False,
    ) -> List[dict[str, Any]]:
        """
        Distinct synced storage names across all nodes (for product config dropdowns).

        When ``backup_capable_only`` is True, prefer PBS rows and any storage whose
        details.content mentions backup.
        """
        rows = db.query(ProxmoxStorage).order_by(ProxmoxStorage.storage_name).all()
        seen: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = (row.storage_name or "").strip()
            if not name or name in seen:
                continue
            stype = (row.storage_type or "").strip().lower()
            details = row.details if isinstance(row.details, dict) else {}
            content = str(details.get("content") or details.get("content_types") or "").lower()
            is_backup = stype == "pbs" or "backup" in content
            if backup_capable_only and not is_backup:
                continue
            seen[name] = {
                "storage_name": name,
                "storage_type": row.storage_type,
                "backup_capable": is_backup,
            }
        return list(seen.values())

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
    def find_template_in_cluster(
        db: Session,
        *,
        cluster_id: int,
        template_name: str,
    ) -> Optional[Tuple[str, int]]:
        """
        Resolve a synced template name to ``(node_name, vmid)`` anywhere in the cluster.
        Prefer an enabled node when multiple copies exist.
        """
        row = (
            db.query(ProxmoxTemplate, ProxmoxNode)
            .join(ProxmoxNode, ProxmoxTemplate.node_id == ProxmoxNode.id)
            .filter(
                ProxmoxNode.cluster_id == cluster_id,
                ProxmoxTemplate.name == template_name,
            )
            .order_by(ProxmoxNode.enabled.desc(), ProxmoxNode.node_name.asc())
            .first()
        )
        if not row:
            return None
        tmpl, node = row
        return str(node.node_name), int(tmpl.vmid)

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
