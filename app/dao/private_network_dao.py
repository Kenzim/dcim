"""Data access for client private networks."""

from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models.private_network import (
    AttachmentState,
    PrivateNetwork,
    PrivateNetworkAttachment,
    PrivateNetworkPool,
    PrivateNetworkStatus,
    SegmentationType,
)


class PrivateNetworkDAO:
    # --- pools ---

    @staticmethod
    def list_pools(db: Session) -> List[PrivateNetworkPool]:
        return db.query(PrivateNetworkPool).order_by(PrivateNetworkPool.location_id).all()

    @staticmethod
    def get_pool(db: Session, pool_id: int) -> Optional[PrivateNetworkPool]:
        return db.query(PrivateNetworkPool).filter(PrivateNetworkPool.id == pool_id).first()

    @staticmethod
    def get_pool_for_location(
        db: Session, location_id: int, *, for_update: bool = False
    ) -> Optional[PrivateNetworkPool]:
        query = db.query(PrivateNetworkPool).filter(PrivateNetworkPool.location_id == location_id)
        if for_update:
            query = query.with_for_update()
        return query.first()

    @staticmethod
    def create_pool(db: Session, **fields) -> PrivateNetworkPool:
        pool = PrivateNetworkPool(**fields)
        db.add(pool)
        db.commit()
        db.refresh(pool)
        return pool

    @staticmethod
    def save(db: Session, row):
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete_pool(db: Session, pool: PrivateNetworkPool) -> None:
        db.delete(pool)
        db.commit()

    # --- networks ---

    @staticmethod
    def get_network(db: Session, network_id: int) -> Optional[PrivateNetwork]:
        return (
            db.query(PrivateNetwork)
            .options(joinedload(PrivateNetwork.attachments))
            .filter(PrivateNetwork.id == network_id)
            .first()
        )

    @staticmethod
    def list_networks(
        db: Session,
        *,
        owner_user_id: Optional[int] = None,
        location_id: Optional[int] = None,
    ) -> List[PrivateNetwork]:
        query = db.query(PrivateNetwork).options(joinedload(PrivateNetwork.attachments))
        if owner_user_id is not None:
            query = query.filter(PrivateNetwork.owner_user_id == owner_user_id)
        if location_id is not None:
            query = query.filter(PrivateNetwork.location_id == location_id)
        return query.order_by(PrivateNetwork.id).all()

    @staticmethod
    def count_for_owner(db: Session, owner_user_id: int, location_id: int) -> int:
        return (
            db.query(PrivateNetwork)
            .filter(
                PrivateNetwork.owner_user_id == owner_user_id,
                PrivateNetwork.location_id == location_id,
            )
            .count()
        )

    @staticmethod
    def count_for_location(db: Session, location_id: int) -> int:
        return db.query(PrivateNetwork).filter(PrivateNetwork.location_id == location_id).count()

    @staticmethod
    def used_segment_ids(db: Session, location_id: int, segmentation_type: SegmentationType) -> set:
        rows = (
            db.query(PrivateNetwork.segment_id)
            .filter(
                PrivateNetwork.location_id == location_id,
                PrivateNetwork.segmentation_type == segmentation_type,
            )
            .all()
        )
        return {row[0] for row in rows}

    @staticmethod
    def list_unsynced(db: Session) -> List[PrivateNetwork]:
        return (
            db.query(PrivateNetwork)
            .filter(
                PrivateNetwork.status == PrivateNetworkStatus.ACTIVE,
                PrivateNetwork.switch_synced.is_(False),
            )
            .all()
        )

    @staticmethod
    def list_deleting(db: Session) -> List[PrivateNetwork]:
        return db.query(PrivateNetwork).filter(PrivateNetwork.status == PrivateNetworkStatus.DELETING).all()

    # --- attachments ---

    @staticmethod
    def get_attachment(db: Session, attachment_id: int) -> Optional[PrivateNetworkAttachment]:
        return (
            db.query(PrivateNetworkAttachment)
            .filter(PrivateNetworkAttachment.id == attachment_id)
            .first()
        )

    @staticmethod
    def list_attachments(db: Session) -> List[PrivateNetworkAttachment]:
        return db.query(PrivateNetworkAttachment).order_by(PrivateNetworkAttachment.id).all()

    @staticmethod
    def attachments_for_service(db: Session, service_id: int) -> List[PrivateNetworkAttachment]:
        return (
            db.query(PrivateNetworkAttachment)
            .filter(PrivateNetworkAttachment.service_id == service_id)
            .order_by(PrivateNetworkAttachment.id)
            .all()
        )

    @staticmethod
    def attachments_for_server(db: Session, server_id: int) -> List[PrivateNetworkAttachment]:
        return (
            db.query(PrivateNetworkAttachment)
            .filter(PrivateNetworkAttachment.server_id == server_id)
            .order_by(PrivateNetworkAttachment.id)
            .all()
        )

    @staticmethod
    def server_has_attachments(db: Session, server_id: int) -> bool:
        return (
            db.query(PrivateNetworkAttachment.id)
            .filter(PrivateNetworkAttachment.server_id == server_id)
            .first()
            is not None
        )

    @staticmethod
    def delete_attachment(db: Session, attachment: PrivateNetworkAttachment) -> None:
        db.delete(attachment)
        db.commit()

    @staticmethod
    def wanted_states() -> tuple:
        """States in which the segment should be present on the VM or port."""
        return (AttachmentState.PENDING, AttachmentState.ACTIVE, AttachmentState.ERROR)
