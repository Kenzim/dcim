"""Private network sharing, client side: invitations, members and the member view of a shared network.

Owner routes keep to the owner (``require_owned``); member routes live under
``/shared`` and only ever match a network the caller has joined. Both are
mounted before the owner router in ``app.main`` so the fixed paths here win over
``/{network_id}``.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.private_networks import (
    AttachBody,
    AttachmentResponse,
    DbDep,
    UserDep,
    _attachment_out,
    _client_user_id,
    _raise,
)
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.storage_dao import StorageDAO
from app.models.private_network import NetworkKind, PrivateNetwork, PrivateNetworkInvitation
from app.models.service import ServiceType
from app.models.storage import ShareState
from app.services import private_network_sharing as sharing
from app.services.audit_service import AuditService
from app.services.private_network_service import PrivateNetworkError, PrivateNetworkService, storage_endpoint_ip
from app.services.storage_service import share_status, tenant_status

client_router = APIRouter(prefix="/client/private-networks", tags=["client"])


class InviteBody(BaseModel):
    invitee: str = Field(..., min_length=2, max_length=255, description="An email address or a username")


class InvitationOut(BaseModel):
    id: int
    invitee: str
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class IncomingInvitation(BaseModel):
    id: int
    network_name: str
    location_id: int
    location_name: Optional[str] = None
    owner: str
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class SharedNetworkResponse(BaseModel):
    """A shared network as a member sees it: no VLAN, switch state or owner details beyond a display name."""

    id: int
    name: str
    location_id: int
    location_name: Optional[str] = None
    cidr: str
    storage_endpoint_ip: str
    status: str
    # Only when the member has a dedicated server on the network: the VLAN ID to tag on its main network card.
    server_vlan_id: Optional[int] = None
    owner: Optional[str] = None
    role: str = "member"
    attachments: List[AttachmentResponse]


class SharedShare(BaseModel):
    id: int
    name: str
    read_only: bool
    status: str
    mount_command: Optional[str] = None
    fstab_line: Optional[str] = None


class SharedStorage(BaseModel):
    linked: bool
    endpoint_ip: Optional[str] = None
    status: Optional[str] = None
    shares: List[SharedShare] = []


def _invitation_out(invitation: PrivateNetworkInvitation) -> InvitationOut:
    return InvitationOut(
        id=invitation.id,
        invitee=invitation.invitee_label,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
    )


def _shared_out(network: PrivateNetwork, user_id: int) -> SharedNetworkResponse:
    prefix_length = ipaddress.ip_network(network.cidr).prefixlen
    own = [
        a
        for a in network.attachments
        if a.proxy_runner_id is None and a.service is not None and a.service.owner_user_id == user_id
    ]
    owner = network.owner_user
    has_dedicated = any(a.service.service_type == ServiceType.BARE_METAL for a in own)
    return SharedNetworkResponse(
        server_vlan_id=network.segment_id if has_dedicated else None,
        id=network.id,
        name=network.name,
        location_id=network.location_id,
        location_name=network.location.name if network.location else None,
        cidr=network.cidr,
        storage_endpoint_ip=storage_endpoint_ip(network.cidr),
        status=network.status.value,
        owner=sharing.display_name(owner),
        attachments=[_attachment_out(a, prefix_length, staff=False) for a in own],
    )


def _member_network(db: Session, user_id: int, network_id: int) -> PrivateNetwork:
    try:
        return sharing.require_member_network(db, user_id, network_id)
    except PrivateNetworkError as exc:
        _raise(exc)


def shared_storage(db: Session, network: PrivateNetwork) -> SharedStorage:
    """The storage address and the shares open to the whole network: nothing about quota, SFTP or restricted shares."""
    tenant = StorageDAO.get_tenant_for_network(db, network.id)
    if tenant is None or tenant.endpoint_ip is None:
        return SharedStorage(linked=False)
    storage_state, _ = tenant_status(db, tenant)
    endpoint = tenant.endpoint_ip
    shares = [
        SharedShare(
            id=share.id,
            name=share.name,
            read_only=bool(share.read_only),
            status=share_status(share, storage_state),
            mount_command=f"mount -t nfs4 {endpoint}:/{share.name} /mnt/{share.name}",
            fstab_line=f"{endpoint}:/{share.name} /mnt/{share.name} nfs4 defaults,_netdev 0 0",
        )
        for share in tenant.shares
        if share.state == ShareState.ACTIVE and share.all_clients
    ]
    return SharedStorage(linked=True, endpoint_ip=endpoint, status=storage_state, shares=shares)


# --- invitee ---


@client_router.get("/invitations", response_model=List[IncomingInvitation])
async def my_invitations(auth: UserDep, db: DbDep):
    return sharing.pending_invitations_out(db, _client_user_id(auth))


@client_router.post("/invitations/{invitation_id}/accept", response_model=SharedNetworkResponse)
async def accept_invitation(invitation_id: int, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    try:
        member = await sharing.respond(db, invitation_id, user_id, True)
    except PrivateNetworkError as exc:
        _raise(exc)
    db.expire_all()
    return _shared_out(PrivateNetworkDAO.get_network(db, member.network_id), user_id)


@client_router.post("/invitations/{invitation_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_invitation(invitation_id: int, auth: UserDep, db: DbDep):
    try:
        await sharing.respond(db, invitation_id, _client_user_id(auth), False)
    except PrivateNetworkError as exc:
        _raise(exc)


# --- member ---


@client_router.get("/shared", response_model=List[SharedNetworkResponse])
async def shared_with_me(auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    return [_shared_out(n, user_id) for n in sharing.shared_networks_for(db, user_id)]


@client_router.get("/shared/{network_id}", response_model=SharedNetworkResponse)
async def shared_network(network_id: int, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    return _shared_out(_member_network(db, user_id, network_id), user_id)


@client_router.get("/shared/{network_id}/storage", response_model=SharedStorage)
async def shared_network_storage(network_id: int, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    return shared_storage(db, _member_network(db, user_id, network_id))


@client_router.post("/shared/{network_id}/attachments", response_model=SharedNetworkResponse)
async def shared_attach(network_id: int, body: AttachBody, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    network = _member_network(db, user_id, network_id)
    try:
        attachment = await PrivateNetworkService.attach(
            db, network.id, body.service_id, ip_address=body.ip_address, acting_user_id=user_id
        )
    except PrivateNetworkError as exc:
        _raise(exc)
    AuditService.log(
        db,
        actor_user_id=user_id,
        subject_user_id=user_id,
        action="private_network.attach",
        resource_type="private_network",
        resource_id=str(network.id),
        metadata={"network_id": network.id, "service_id": body.service_id, "attachment_id": attachment.id},
    )
    db.commit()
    db.expire_all()
    return _shared_out(PrivateNetworkDAO.get_network(db, network.id), user_id)


@client_router.delete("/shared/{network_id}/attachments/{attachment_id}", response_model=SharedNetworkResponse)
async def shared_detach(network_id: int, attachment_id: int, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    network = _member_network(db, user_id, network_id)
    if not any(a.id == attachment_id and a.proxy_runner_id is None for a in network.attachments):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    try:
        await PrivateNetworkService.detach(db, attachment_id, acting_user_id=user_id)
    except PrivateNetworkError as exc:
        _raise(exc)
    AuditService.log(
        db,
        actor_user_id=user_id,
        subject_user_id=user_id,
        action="private_network.detach",
        resource_type="private_network",
        resource_id=str(network.id),
        metadata={"network_id": network.id, "attachment_id": attachment_id},
    )
    db.commit()
    db.expire_all()
    return _shared_out(PrivateNetworkDAO.get_network(db, network.id), user_id)


@client_router.post("/shared/{network_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
async def shared_leave(network_id: int, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    try:
        await sharing.remove_member(db, network_id, user_id, by="leave", actor_user_id=user_id)
    except PrivateNetworkError as exc:
        _raise(exc)


# --- owner ---


def _owned(db: Session, user_id: int, network_id: int) -> PrivateNetwork:
    try:
        return sharing.require_owned(db, user_id, network_id)
    except PrivateNetworkError as exc:
        _raise(exc)


@client_router.post(
    "/{network_id}/invitations", response_model=InvitationOut, status_code=status.HTTP_201_CREATED
)
async def invite(network_id: int, body: InviteBody, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    _owned(db, user_id, network_id)
    try:
        invitation = await sharing.invite(db, network_id, user_id, body.invitee)
    except PrivateNetworkError as exc:
        _raise(exc)
    return _invitation_out(invitation)


@client_router.delete("/{network_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_invitation(network_id: int, invitation_id: int, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    _owned(db, user_id, network_id)
    try:
        await sharing.cancel_invitation(db, network_id, invitation_id, user_id)
    except PrivateNetworkError as exc:
        _raise(exc)


@client_router.delete("/{network_id}/members/{member_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(network_id: int, member_user_id: int, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    _owned(db, user_id, network_id)
    try:
        await sharing.remove_member(db, network_id, member_user_id, by="owner", actor_user_id=user_id)
    except PrivateNetworkError as exc:
        _raise(exc)
