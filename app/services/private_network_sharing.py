"""Private network sharing: invitations, members and what happens when sharing ends.

The owner of a standard private network invites other clients (see
``docs/private-network-sharing.md``). A member can attach their own services
and use the open storage shares; everything that manages the network stays with
the owner. Every function that changes anything takes the same lock as
``PrivateNetworkService``, so a member removal and an attach never interleave.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.storage_dao import StorageDAO
from app.models.private_network import (
    InvitationStatus,
    NetworkKind,
    PrivateNetwork,
    PrivateNetworkInvitation,
    PrivateNetworkMember,
    PrivateNetworkStatus,
)
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User
from app.services import private_network_service as pns
from app.services.audit_service import AuditService
from app.services.private_network_service import PrivateNetworkError, PrivateNetworkService

logger = logging.getLogger(__name__)

NOTIFY_CATEGORY = "network_sharing"
ANSWERED_KEEP = timedelta(days=90)

NOT_FOUND = "Private network not found"
INVITATION_NOT_FOUND = "Invitation not found"

MESSAGES = {
    "invited": "You have a new invitation to join a private network. Sign in to the portal to answer it.",
    "accepted": "A client accepted your invitation to a private network.",
    "declined": "A client declined your invitation to a private network.",
    "left": "A member left one of your shared private networks.",
    "removed": "You were removed from a shared private network, and your servers were taken off it.",
    "ended": "A shared private network you belonged to has ended, and your servers were taken off it.",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def is_client(user: Optional[User]) -> bool:
    """Staff and reseller accounts are never clients."""
    return user is not None and not user.is_admin and not user.is_reseller


def same_scope(owner: User, other: User) -> bool:
    """Sharing never crosses a reseller boundary: both accounts have the same ``reseller_id`` (or none)."""
    return owner.reseller_id == other.reseller_id


def _eligible(owner: Optional[User], invitee: Optional[User]) -> bool:
    return (
        is_client(owner)
        and is_client(invitee)
        and owner.id != invitee.id
        and same_scope(owner, invitee)
    )


def _notify(db: Session, user_id: Optional[int], kind: str, key: str) -> None:
    """Queue an opt-in notice with a fixed sentence. Never raises."""
    if user_id is None:
        return
    try:
        from app.services.client_notifications import notify

        notify(db, user_id, NOTIFY_CATEGORY, MESSAGES[kind], event_key=key)
    except Exception:  # noqa: BLE001 - a notice never breaks the action
        logger.warning("Private network sharing: notice %s failed", kind, exc_info=True)


def _audit(
    db: Session,
    action: str,
    network: PrivateNetwork,
    *,
    actor: Optional[int],
    subject: Optional[int],
    other_user_id: Optional[int] = None,
    **extra,
) -> None:
    metadata = {"network_id": network.id}
    if other_user_id is not None:
        metadata["user_id"] = other_user_id
    metadata.update(extra)
    AuditService.log(
        db,
        actor_user_id=actor,
        subject_user_id=subject,
        action=f"private_network.{action}",
        resource_type="private_network",
        resource_id=str(network.id),
        metadata=metadata,
    )


def member_limit(db: Session, network: PrivateNetwork) -> int:
    pool = PrivateNetworkDAO.get_pool_for_location(db, network.location_id)
    return max(0, int(pool.max_members_per_network)) if pool is not None else 0


def _shareable(network: Optional[PrivateNetwork]) -> bool:
    return (
        network is not None
        and network.kind == NetworkKind.STANDARD
        and network.status == PrivateNetworkStatus.ACTIVE
        and network.owner_user_id is not None
    )


# ------------------------------------------------------------------ access


def require_owned(db: Session, user_id: int, network_id: int) -> PrivateNetwork:
    """The network when ``user_id`` owns it. Anyone else gets "not found"."""
    network = PrivateNetworkDAO.get_network(db, network_id)
    if network is None or network.owner_user_id is None or network.owner_user_id != user_id:
        raise PrivateNetworkError(NOT_FOUND, status_code=404)
    return network


def require_member_network(db: Session, user_id: int, network_id: int) -> PrivateNetwork:
    """The network when ``user_id`` is a current member of it. Anyone else gets "not found"."""
    network = PrivateNetworkDAO.get_network(db, network_id)
    if (
        network is None
        or network.kind != NetworkKind.STANDARD
        or network.owner_user_id == user_id
        or PrivateNetworkDAO.get_member(db, network.id, user_id) is None
    ):
        raise PrivateNetworkError(NOT_FOUND, status_code=404)
    return network


def is_member(db: Session, user_id: int, network_id: int) -> bool:
    return PrivateNetworkDAO.get_member(db, network_id, user_id) is not None


# ------------------------------------------------------------------ invitations


def _expire_stale(db: Session, network_id: Optional[int] = None) -> int:
    now = _now()
    rows = [
        inv
        for inv in PrivateNetworkDAO.list_pending_invitations(db)
        if (network_id is None or inv.network_id == network_id) and _aware(inv.expires_at) <= now
    ]
    for inv in rows:
        inv.status = InvitationStatus.EXPIRED
        inv.active_key = None
        inv.responded_at = now
    if rows:
        db.commit()
    return len(rows)


def _find_user(db: Session, label: str) -> Optional[User]:
    """An account whose email, else username, equals ``label`` (case-insensitive)."""
    for column in (User.email, User.username):
        user = db.query(User).filter(func.lower(column) == label).first()
        if user is not None:
            return user
    return None


async def invite(db: Session, network_id: int, owner_user_id: int, who: str) -> PrivateNetworkInvitation:
    """Record an invitation to an email address or username.

    The answer never depends on whether an account matches. An invitation that
    matches no eligible client is stored the same way and never shown to anyone
    but the owner.
    """
    label = (who or "").strip().lower()
    if not 2 <= len(label) <= 255:
        raise PrivateNetworkError("Enter an email address or a username")
    async with pns._ops_lock:
        network = require_owned(db, owner_user_id, network_id)
        if not _shareable(network):
            raise PrivateNetworkError("This network cannot be shared", status_code=409)
        limit = member_limit(db, network)
        if limit <= 0:
            raise PrivateNetworkError("Sharing is not available in this location", status_code=409)
        _expire_stale(db, network.id)
        if PrivateNetworkDAO.count_members(db, network.id) + PrivateNetworkDAO.count_pending_invitations(
            db, network.id
        ) >= limit:
            raise PrivateNetworkError(
                f"A network can have at most {limit} members including pending invitations", status_code=409
            )
        owner = db.get(User, owner_user_id)
        invitee = _find_user(db, label)
        if invitee is not None and (
            not _eligible(owner, invitee) or PrivateNetworkDAO.get_member(db, network.id, invitee.id) is not None
        ):
            invitee = None
        key = f"{network.id}:{invitee.id if invitee is not None else 'x:' + label}"[:300]
        existing = PrivateNetworkDAO.pending_invitation_by_key(db, key)
        if existing is not None:
            return existing
        invitation = PrivateNetworkInvitation(
            network_id=network.id,
            inviter_user_id=owner_user_id,
            invitee_user_id=invitee.id if invitee is not None else None,
            invitee_label=label,
            status=InvitationStatus.PENDING,
            active_key=key,
            expires_at=_now() + timedelta(days=max(1, int(settings.private_network_invitation_days))),
        )
        db.add(invitation)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            existing = PrivateNetworkDAO.pending_invitation_by_key(db, key)
            if existing is not None:
                return existing
            raise PrivateNetworkError("Could not send the invitation, please try again", status_code=409) from None
        _audit(db, "invite", network, actor=owner_user_id, subject=owner_user_id, invitation_id=invitation.id)
        if invitee is not None:
            _notify(db, invitee.id, "invited", f"pn-invite:{invitation.id}")
        db.commit()
        db.refresh(invitation)
        return invitation


async def cancel_invitation(db: Session, network_id: int, invitation_id: int, owner_user_id: int) -> None:
    async with pns._ops_lock:
        network = require_owned(db, owner_user_id, network_id)
        invitation = PrivateNetworkDAO.get_invitation(db, invitation_id)
        if (
            invitation is None
            or invitation.network_id != network.id
            or invitation.status != InvitationStatus.PENDING
        ):
            raise PrivateNetworkError(INVITATION_NOT_FOUND, status_code=404)
        invitation.status = InvitationStatus.CANCELLED
        invitation.active_key = None
        invitation.responded_at = _now()
        _audit(db, "invitation_cancel", network, actor=owner_user_id, subject=owner_user_id, invitation_id=invitation.id)
        db.commit()


async def respond(db: Session, invitation_id: int, user_id: int, accept: bool) -> Optional[PrivateNetworkMember]:
    """The invitee accepts or declines. Anyone else gets "not found"."""
    async with pns._ops_lock:
        invitation = PrivateNetworkDAO.get_invitation(db, invitation_id)
        if (
            invitation is None
            or invitation.invitee_user_id != user_id
            or invitation.status != InvitationStatus.PENDING
        ):
            raise PrivateNetworkError(INVITATION_NOT_FOUND, status_code=404)
        now = _now()
        network = invitation.network
        owner = db.get(User, network.owner_user_id) if network.owner_user_id else None
        invitee = db.get(User, user_id)
        if _aware(invitation.expires_at) <= now or not _shareable(network) or not _eligible(owner, invitee):
            invitation.status = InvitationStatus.EXPIRED
            invitation.active_key = None
            invitation.responded_at = now
            db.commit()
            raise PrivateNetworkError(INVITATION_NOT_FOUND, status_code=404)
        if not accept:
            invitation.status = InvitationStatus.DECLINED
            invitation.active_key = None
            invitation.responded_at = now
            _audit(db, "invitation_decline", network, actor=user_id, subject=owner.id, other_user_id=user_id)
            _notify(db, owner.id, "declined", f"pn-decline:{invitation.id}")
            db.commit()
            return None
        if PrivateNetworkDAO.count_members(db, network.id) >= member_limit(db, network):
            raise PrivateNetworkError("This network has no room for another member", status_code=409)
        member = PrivateNetworkMember(
            network_id=network.id, user_id=user_id, invited_by_user_id=invitation.inviter_user_id
        )
        db.add(member)
        invitation.status = InvitationStatus.ACCEPTED
        invitation.active_key = None
        invitation.responded_at = now
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise PrivateNetworkError("You are already a member of this network", status_code=409) from None
        _audit(db, "invitation_accept", network, actor=user_id, subject=owner.id, other_user_id=user_id)
        _notify(db, owner.id, "accepted", f"pn-accept:{invitation.id}")
        db.commit()
        db.refresh(member)
        return member


# ------------------------------------------------------------------ removal


def _scrub_listed_addresses(db: Session, network_id: int, addresses: set) -> None:
    """Take addresses off the restricted shares of the storage linked to the network."""
    if not addresses:
        return
    tenant = StorageDAO.get_tenant_for_network(db, network_id)
    if tenant is None:
        return
    for share in tenant.shares:
        listed = list(share.clients or [])
        kept = [address for address in listed if address not in addresses]
        if kept != listed:
            share.clients = kept
    db.commit()


async def _remove_member_locked(db: Session, network: PrivateNetwork, user_id: int) -> bool:
    """Delete the membership first (so access ends at once), then detach every attachment of the member."""
    member = PrivateNetworkDAO.get_member(db, network.id, user_id)
    if member is None:
        return False
    db.delete(member)
    for invitation in list(network.invitations):
        if invitation.invitee_user_id == user_id and invitation.status == InvitationStatus.PENDING:
            invitation.status = InvitationStatus.CANCELLED
            invitation.active_key = None
    db.commit()
    db.expire(network)
    freed = set()
    for attachment in list(network.attachments):
        service = attachment.service
        if service is None or service.service_type == ServiceType.NFS_STORAGE or service.owner_user_id != user_id:
            continue
        if attachment.ip_address:
            freed.add(attachment.ip_address)
        await PrivateNetworkService._detach_locked(db, attachment)
    _scrub_listed_addresses(db, network.id, freed)
    db.expire(network)
    return True


async def remove_member(
    db: Session, network_id: int, user_id: int, *, by: str, actor_user_id: Optional[int] = None
) -> None:
    """Remove a member. ``by`` is ``owner``, ``staff`` or ``leave`` (the member left)."""
    async with pns._ops_lock:
        network = PrivateNetworkDAO.get_network(db, network_id)
        if network is None:
            raise PrivateNetworkError(NOT_FOUND, status_code=404)
        if by == "leave":
            if not is_member(db, user_id, network.id):
                raise PrivateNetworkError(NOT_FOUND, status_code=404)
        elif by == "owner":
            if network.owner_user_id is None or network.owner_user_id != actor_user_id:
                raise PrivateNetworkError(NOT_FOUND, status_code=404)
        if not await _remove_member_locked(db, network, user_id):
            raise PrivateNetworkError("Member not found", status_code=404)
        if by == "leave":
            _audit(db, "leave", network, actor=user_id, subject=network.owner_user_id, other_user_id=user_id)
            _notify(db, network.owner_user_id, "left", f"pn-left:{network.id}:{user_id}:{_now().timestamp()}")
        else:
            _audit(
                db,
                "member_remove",
                network,
                actor=actor_user_id,
                subject=network.owner_user_id,
                other_user_id=user_id,
                by=by,
            )
            _notify(db, user_id, "removed", f"pn-removed:{network.id}:{user_id}:{_now().timestamp()}")
        db.commit()


async def end_sharing_locked(db: Session, network: PrivateNetwork, *, reason: str) -> int:
    """Remove every member and cancel every pending invitation. The caller holds the lock."""
    removed = 0
    for member in list(network.members):
        user_id = member.user_id
        if await _remove_member_locked(db, network, user_id):
            removed += 1
            _audit(db, "share_end", network, actor=None, subject=network.owner_user_id, other_user_id=user_id, reason=reason)
            _notify(db, user_id, "ended", f"pn-ended:{network.id}:{user_id}:{_now().timestamp()}")
    for invitation in list(network.invitations):
        if invitation.status == InvitationStatus.PENDING:
            invitation.status = InvitationStatus.CANCELLED
            invitation.active_key = None
            invitation.responded_at = _now()
    network.owner_gone_since = None
    db.commit()
    return removed


# ------------------------------------------------------------------ repair loop


def _owner_has_live_service(db: Session, owner_user_id: int) -> bool:
    return (
        db.query(Service)
        .filter(Service.owner_user_id == owner_user_id, Service.status != ServiceStatus.TERMINATED)
        .first()
        is not None
    )


async def reconcile_sharing(db: Session) -> dict:
    """Expire invitations, drop members who stopped being eligible and wind up abandoned networks."""
    counts = {"expired": 0, "removed": 0, "ended": 0}
    async with pns._ops_lock:
        counts["expired"] = _expire_stale(db)
        cutoff = _now() - ANSWERED_KEEP
        for inv in (
            db.query(PrivateNetworkInvitation)
            .filter(PrivateNetworkInvitation.status != InvitationStatus.PENDING)
            .all()
        ):
            if inv.responded_at is not None and _aware(inv.responded_at) < cutoff:
                db.delete(inv)
        db.commit()

    for network_id in [n.id for n in PrivateNetworkDAO.list_networks_with_members(db)]:
        async with pns._ops_lock:
            db.expire_all()
            network = PrivateNetworkDAO.get_network(db, network_id)
            if network is None or not network.members:
                continue
            owner = db.get(User, network.owner_user_id) if network.owner_user_id else None
            if owner is None or network.kind != NetworkKind.STANDARD:
                counts["removed"] += await end_sharing_locked(db, network, reason="owner_gone")
                counts["ended"] += 1
                await _wind_up_ownerless(db, network)
                continue
            for member in list(network.members):
                if not _eligible(owner, db.get(User, member.user_id)):
                    if await _remove_member_locked(db, network, member.user_id):
                        counts["removed"] += 1
                        _audit(db, "member_remove", network, actor=None, subject=owner.id,
                               other_user_id=member.user_id, by="repair")
                        _notify(db, member.user_id, "removed", f"pn-removed:{network.id}:{member.user_id}:{_now().timestamp()}")
                        db.commit()
            if not network.members:
                continue
            if _owner_has_live_service(db, owner.id):
                if network.owner_gone_since is not None:
                    network.owner_gone_since = None
                    db.commit()
                continue
            if network.owner_gone_since is None:
                network.owner_gone_since = _now()
                db.commit()
                continue
            grace = timedelta(days=max(0, int(settings.private_network_owner_gone_grace_days)))
            if _now() - _aware(network.owner_gone_since) >= grace:
                counts["removed"] += await end_sharing_locked(db, network, reason="owner_gone")
                counts["ended"] += 1
    return counts


async def _wind_up_ownerless(db: Session, network: PrivateNetwork) -> None:
    """A network whose owner account is gone: delete it unless storage is still linked to it."""
    if network.owner_user_id is not None or network.status != PrivateNetworkStatus.ACTIVE:
        return
    if StorageDAO.get_tenant_for_network(db, network.id) is not None:
        return
    network.status = PrivateNetworkStatus.DELETING
    PrivateNetworkDAO.save(db, network)
    db.expire(network)
    for attachment in list(network.attachments):
        await PrivateNetworkService._detach_locked(db, attachment)
    db.expire(network)
    if not network.attachments:
        await PrivateNetworkService._finish_delete(db, network)


# ------------------------------------------------------------------ views


def _server_kind(service: Optional[Service]) -> str:
    return service.service_type.value if service is not None else "unknown"


def owner_extras(db: Session, network: PrivateNetwork) -> dict:
    """Members (with the addresses of their attached servers) and pending invitations, for the owner."""
    members = []
    for member in network.members:
        servers = [
            {"ip_address": a.ip_address, "kind": _server_kind(a.service), "state": a.state.value}
            for a in network.attachments
            if a.service is not None
            and a.service.service_type != ServiceType.NFS_STORAGE
            and a.service.owner_user_id == member.user_id
        ]
        members.append(
            {
                "user_id": member.user_id,
                "username": member.user.username if member.user is not None else None,
                "joined_at": member.joined_at,
                "servers": servers,
            }
        )
    invitations = [
        {
            "id": inv.id,
            "invitee": inv.invitee_label,
            "created_at": inv.created_at,
            "expires_at": inv.expires_at,
        }
        for inv in network.invitations
        if inv.status == InvitationStatus.PENDING and _aware(inv.expires_at) > _now()
    ]
    return {
        "members": members,
        "invitations": invitations,
        "member_limit": member_limit(db, network),
    }


def pending_invitations_out(db: Session, user_id: int) -> List[dict]:
    """The invitations waiting for ``user_id`` to answer."""
    out = []
    now = _now()
    for inv in PrivateNetworkDAO.pending_invitations_for_user(db, user_id):
        network = inv.network
        if _aware(inv.expires_at) <= now or not _shareable(network):
            continue
        owner = db.get(User, network.owner_user_id)
        if not _eligible(owner, db.get(User, user_id)):
            continue
        out.append(
            {
                "id": inv.id,
                "network_name": network.name,
                "location_id": network.location_id,
                "location_name": network.location.name if network.location is not None else None,
                "owner": owner.username,
                "created_at": inv.created_at,
                "expires_at": inv.expires_at,
            }
        )
    return out


def shared_networks_for(db: Session, user_id: int) -> List[PrivateNetwork]:
    return [n for n in PrivateNetworkDAO.list_member_networks(db, user_id) if n.owner_user_id != user_id]
