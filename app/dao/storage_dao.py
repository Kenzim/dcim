"""Data access for NFS storage nodes, tenants and shares."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.storage import ShareState, StorageNode, StorageShare, StorageTenant, VlanMode

_KEY_PREFIX = "srk_"
ONLINE_THRESHOLD = timedelta(seconds=180)


def generate_storage_node_key() -> str:
    return f"{_KEY_PREFIX}{secrets.token_urlsafe(48)}"


def hash_storage_node_key(api_key: str) -> str:
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StorageDAO:
    @staticmethod
    def save(db: Session, row):
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, row) -> None:
        db.delete(row)
        db.commit()

    # --- nodes ---

    @staticmethod
    def create_node(
        db: Session,
        *,
        name: str,
        server_id: int,
        vlan_mode: VlanMode = VlanMode.SWITCH,
        vlan_port_ids: Optional[list] = None,
        enabled: bool = True,
    ) -> Tuple[StorageNode, str]:
        """Create a node and return ``(row, plaintext_key)``; the key is not stored."""
        plaintext = generate_storage_node_key()
        row = StorageNode(
            name=name.strip(),
            server_id=server_id,
            api_key_hash=hash_storage_node_key(plaintext),
            api_key_prefix=plaintext[:10],
            enabled=enabled,
            vlan_mode=vlan_mode,
            vlan_port_ids=list(vlan_port_ids or []),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row, plaintext

    @staticmethod
    def rotate_node_key(db: Session, row: StorageNode) -> str:
        plaintext = generate_storage_node_key()
        row.api_key_hash = hash_storage_node_key(plaintext)
        row.api_key_prefix = plaintext[:10]
        db.commit()
        db.refresh(row)
        return plaintext

    @staticmethod
    def get_node(db: Session, node_id: int) -> Optional[StorageNode]:
        return db.query(StorageNode).filter(StorageNode.id == node_id).first()

    @staticmethod
    def get_node_by_server(db: Session, server_id: int) -> Optional[StorageNode]:
        return db.query(StorageNode).filter(StorageNode.server_id == server_id).first()

    @staticmethod
    def get_node_by_key(db: Session, api_key: str) -> Optional[StorageNode]:
        if not api_key:
            return None
        digest = hash_storage_node_key(api_key)
        row = db.query(StorageNode).filter(StorageNode.api_key_hash == digest).first()
        if row is None or not secrets.compare_digest(row.api_key_hash, digest):
            return None
        return row

    @staticmethod
    def list_nodes(db: Session) -> List[StorageNode]:
        return db.query(StorageNode).options(joinedload(StorageNode.server)).order_by(StorageNode.id).all()

    @staticmethod
    def touch_node(db: Session, row: StorageNode, client_ip: Optional[str]) -> None:
        row.last_seen_at = _utcnow_naive()
        if client_ip:
            row.last_seen_ip = client_ip[:64]
        db.commit()

    @staticmethod
    def is_online(row: StorageNode, *, now: Optional[datetime] = None) -> bool:
        if not row.last_seen_at:
            return False
        now = now or _utcnow_naive()
        seen = row.last_seen_at.replace(tzinfo=None)
        return (now - seen) <= ONLINE_THRESHOLD

    @staticmethod
    def live_tenant_counts(db: Session) -> Dict[int, int]:
        """Tenants per node that are not terminated."""
        rows = (
            db.query(StorageTenant.node_id, func.count(StorageTenant.id))
            .filter(StorageTenant.node_id.isnot(None), StorageTenant.terminated_at.is_(None))
            .group_by(StorageTenant.node_id)
            .all()
        )
        return {node_id: count for node_id, count in rows}

    @staticmethod
    def count_tenants_on_node(db: Session, node_id: int) -> int:
        return db.query(StorageTenant).filter(StorageTenant.node_id == node_id).count()

    # --- tenants ---

    @staticmethod
    def create_tenant(db: Session, **fields) -> StorageTenant:
        row = StorageTenant(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def get_tenant(db: Session, tenant_id: int) -> Optional[StorageTenant]:
        return (
            db.query(StorageTenant)
            .options(joinedload(StorageTenant.shares))
            .filter(StorageTenant.id == tenant_id)
            .first()
        )

    @staticmethod
    def get_tenant_for_service(db: Session, service_id: int) -> Optional[StorageTenant]:
        return (
            db.query(StorageTenant)
            .options(joinedload(StorageTenant.shares))
            .filter(StorageTenant.service_id == service_id)
            .first()
        )

    @staticmethod
    def get_tenant_for_network(db: Session, network_id: int) -> Optional[StorageTenant]:
        return db.query(StorageTenant).filter(StorageTenant.network_id == network_id).first()

    @staticmethod
    def tenants_for_node(db: Session, node_id: int) -> List[StorageTenant]:
        return (
            db.query(StorageTenant)
            .options(joinedload(StorageTenant.shares), joinedload(StorageTenant.service))
            .filter(StorageTenant.node_id == node_id)
            .order_by(StorageTenant.id)
            .all()
        )

    @staticmethod
    def tenants_for_location(db: Session, location_id: int) -> List[StorageTenant]:
        """Every tenant placed in the location, terminated ones awaiting destruction included."""
        return (
            db.query(StorageTenant)
            .options(joinedload(StorageTenant.shares), joinedload(StorageTenant.service))
            .filter(StorageTenant.location_id == location_id, StorageTenant.node_id.isnot(None))
            .order_by(StorageTenant.id)
            .all()
        )

    @staticmethod
    def tenants_served_by(db: Session, node_id: int) -> List[StorageTenant]:
        return (
            db.query(StorageTenant)
            .options(joinedload(StorageTenant.service))
            .filter(StorageTenant.current_node_id == node_id)
            .order_by(StorageTenant.id)
            .all()
        )

    @staticmethod
    def list_tenants(db: Session, *, owner_user_id: Optional[int] = None) -> List[StorageTenant]:
        from app.models.service import Service

        query = db.query(StorageTenant).options(joinedload(StorageTenant.shares))
        if owner_user_id is not None:
            query = query.join(Service, Service.id == StorageTenant.service_id).filter(
                Service.owner_user_id == owner_user_id
            )
        return query.order_by(StorageTenant.id).all()

    @staticmethod
    def unterminated_tenants(db: Session) -> List[StorageTenant]:
        return db.query(StorageTenant).filter(StorageTenant.terminated_at.is_(None)).all()

    @staticmethod
    def undestroyed_vlan_on_node(db: Session, node_id: int, vlan: int, location_id: Optional[int]) -> Optional[StorageTenant]:
        """A tenant on the node that still uses ``vlan`` (live or awaiting destruction)."""
        query = db.query(StorageTenant).filter(StorageTenant.node_id == node_id, StorageTenant.vlan == vlan)
        if location_id is not None:
            query = query.filter(StorageTenant.location_id == location_id)
        return query.first()

    @staticmethod
    def held_vlans(db: Session, location_id: int) -> set:
        """VLANs in the location still used by a tenant (live or awaiting destruction)."""
        rows = (
            db.query(StorageTenant.vlan)
            .filter(StorageTenant.location_id == location_id, StorageTenant.vlan.isnot(None))
            .all()
        )
        return {row[0] for row in rows}

    # --- shares ---

    @staticmethod
    def get_share(db: Session, share_id: int) -> Optional[StorageShare]:
        return db.query(StorageShare).filter(StorageShare.id == share_id).first()

    @staticmethod
    def active_quota_total(tenant: StorageTenant, *, exclude_share_id: Optional[int] = None) -> int:
        return sum(
            share.quota_bytes
            for share in tenant.shares
            if share.state == ShareState.ACTIVE and share.id != exclude_share_id
        )
