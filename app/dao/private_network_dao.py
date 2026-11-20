"""Data access for client private networks."""

from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models.private_network import (
    AttachmentState,
    NetworkKind,
    InvitationStatus,
    PrivateNetwork,
    PrivateNetworkAttachment,
    PrivateNetworkDhcpLease,
    PrivateNetworkDhcpReservation,
    PrivateNetworkInvitation,
    PrivateNetworkMember,
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
        """Networks that count towards the client's limit (proxy access networks do not)."""
        return (
            db.query(PrivateNetwork)
            .filter(
                PrivateNetwork.owner_user_id == owner_user_id,
                PrivateNetwork.location_id == location_id,
                PrivateNetwork.kind == NetworkKind.STANDARD,
            )
            .count()
        )

    @staticmethod
    def get_access_network(db: Session, owner_user_id: int, location_id: int) -> Optional[PrivateNetwork]:
        """The client's proxy access network in a location, whatever its status."""
        return (
            db.query(PrivateNetwork)
            .filter(
                PrivateNetwork.owner_user_id == owner_user_id,
                PrivateNetwork.location_id == location_id,
                PrivateNetwork.kind == NetworkKind.PROXY_ACCESS,
            )
            .first()
        )

    @staticmethod
    def list_access_networks(
        db: Session, *, location_id: Optional[int] = None, owner_user_id: Optional[int] = None
    ) -> List[PrivateNetwork]:
        query = db.query(PrivateNetwork).options(joinedload(PrivateNetwork.attachments)).filter(
            PrivateNetwork.kind == NetworkKind.PROXY_ACCESS
        )
        if location_id is not None:
            query = query.filter(PrivateNetwork.location_id == location_id)
        if owner_user_id is not None:
            query = query.filter(PrivateNetwork.owner_user_id == owner_user_id)
        return query.order_by(PrivateNetwork.id).all()

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

    # --- members and invitations ---

    @staticmethod
    def get_member(db: Session, network_id: int, user_id: int) -> Optional[PrivateNetworkMember]:
        return (
            db.query(PrivateNetworkMember)
            .filter(PrivateNetworkMember.network_id == network_id, PrivateNetworkMember.user_id == user_id)
            .first()
        )

    @staticmethod
    def count_members(db: Session, network_id: int) -> int:
        return db.query(PrivateNetworkMember).filter(PrivateNetworkMember.network_id == network_id).count()

    @staticmethod
    def list_member_networks(db: Session, user_id: int) -> List[PrivateNetwork]:
        """Standard networks the user has joined as a member (never ones they own)."""
        return (
            db.query(PrivateNetwork)
            .options(joinedload(PrivateNetwork.attachments))
            .join(PrivateNetworkMember, PrivateNetworkMember.network_id == PrivateNetwork.id)
            .filter(
                PrivateNetworkMember.user_id == user_id,
                PrivateNetwork.kind == NetworkKind.STANDARD,
            )
            .order_by(PrivateNetwork.id)
            .all()
        )

    @staticmethod
    def list_member_user_networks(db: Session, user_id: int) -> List[PrivateNetworkMember]:
        return db.query(PrivateNetworkMember).filter(PrivateNetworkMember.user_id == user_id).all()

    @staticmethod
    def list_networks_with_members(db: Session) -> List[PrivateNetwork]:
        return (
            db.query(PrivateNetwork)
            .filter(PrivateNetwork.id.in_(db.query(PrivateNetworkMember.network_id)))
            .order_by(PrivateNetwork.id)
            .all()
        )

    @staticmethod
    def get_invitation(db: Session, invitation_id: int) -> Optional[PrivateNetworkInvitation]:
        return (
            db.query(PrivateNetworkInvitation).filter(PrivateNetworkInvitation.id == invitation_id).first()
        )

    @staticmethod
    def count_pending_invitations(db: Session, network_id: int) -> int:
        return (
            db.query(PrivateNetworkInvitation)
            .filter(
                PrivateNetworkInvitation.network_id == network_id,
                PrivateNetworkInvitation.status == InvitationStatus.PENDING,
            )
            .count()
        )

    @staticmethod
    def pending_invitations_for_user(db: Session, user_id: int) -> List[PrivateNetworkInvitation]:
        return (
            db.query(PrivateNetworkInvitation)
            .filter(
                PrivateNetworkInvitation.invitee_user_id == user_id,
                PrivateNetworkInvitation.status == InvitationStatus.PENDING,
            )
            .order_by(PrivateNetworkInvitation.id)
            .all()
        )

    @staticmethod
    def pending_invitation_by_key(
        db: Session, active_key: str
    ) -> Optional[PrivateNetworkInvitation]:
        return (
            db.query(PrivateNetworkInvitation)
            .filter(
                PrivateNetworkInvitation.active_key == active_key,
                PrivateNetworkInvitation.status == InvitationStatus.PENDING,
            )
            .first()
        )

    @staticmethod
    def list_pending_invitations(db: Session) -> List[PrivateNetworkInvitation]:
        return (
            db.query(PrivateNetworkInvitation)
            .filter(PrivateNetworkInvitation.status == InvitationStatus.PENDING)
            .all()
        )

    # --- DHCP ---

    @staticmethod
    def get_dhcp_reservation(
        db: Session, network_id: int, reservation_id: int
    ) -> Optional[PrivateNetworkDhcpReservation]:
        return (
            db.query(PrivateNetworkDhcpReservation)
            .filter(
                PrivateNetworkDhcpReservation.id == reservation_id,
                PrivateNetworkDhcpReservation.network_id == network_id,
            )
            .first()
        )

    @staticmethod
    def list_dhcp_reservations(db: Session, network_id: int) -> List[PrivateNetworkDhcpReservation]:
        return (
            db.query(PrivateNetworkDhcpReservation)
            .filter(PrivateNetworkDhcpReservation.network_id == network_id)
            .order_by(PrivateNetworkDhcpReservation.id)
            .all()
        )

    @staticmethod
    def list_dhcp_leases(db: Session, network_id: int) -> List[PrivateNetworkDhcpLease]:
        return (
            db.query(PrivateNetworkDhcpLease)
            .filter(PrivateNetworkDhcpLease.network_id == network_id)
            .order_by(PrivateNetworkDhcpLease.id)
            .all()
        )

    @staticmethod
    def list_presence_networks(db: Session, location_id: int) -> List[PrivateNetwork]:
        """Active standard networks of a location, the candidates for a storage presence."""
        return (
            db.query(PrivateNetwork)
            .filter(
                PrivateNetwork.location_id == location_id,
                PrivateNetwork.kind == NetworkKind.STANDARD,
                PrivateNetwork.status == PrivateNetworkStatus.ACTIVE,
            )
            .order_by(PrivateNetwork.id)
            .all()
        )

    @staticmethod
    def networks_with_presence_on(db: Session, node_id: int) -> List[PrivateNetwork]:
        return db.query(PrivateNetwork).filter(PrivateNetwork.presence_node_id == node_id).all()

    @staticmethod
    def wanted_states() -> tuple:
        """States in which the segment should be present on the VM or port."""
        return (AttachmentState.PENDING, AttachmentState.ACTIVE, AttachmentState.ERROR)
