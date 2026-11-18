"""Shared pieces of the NFS storage API: request bodies, response shapes and share operations.

Used by the staff routes (``app.api.storage``) and the client routes
(``app.api.storage_client``); ``staff`` controls whether node and runner
details are included.
"""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.service import Service
from app.models.storage import StorageShare, StorageTenant
from app.services import storage_sftp
from app.services.storage_service import (
    GIB,
    StorageError,
    StorageService,
    iso_utc,
    share_clients,
    share_status,
    storage_attachment,
    tenant_status,
)

DbDep = Annotated[Session, Depends(get_db)]

STORAGE_NOT_FOUND = "Storage not found"


class LinkBody(BaseModel):
    """Link to ``network_id``, or leave it out to create a new network (optional name and CIDR)."""

    network_id: Optional[int] = None
    network_name: Optional[str] = Field(None, max_length=64)
    cidr: Optional[str] = Field(None, max_length=32)
    location_id: Optional[int] = None


SftpAccessValue = Literal["off", "ro", "rw"]


class ShareCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)
    quota_gb: int = Field(..., ge=1)
    clients: List[str] = Field(default_factory=list, max_length=256)
    all_clients: bool = False
    read_only: bool = False
    sftp_access: SftpAccessValue = "off"


class ShareUpdate(BaseModel):
    quota_gb: Optional[int] = Field(None, ge=1)
    clients: Optional[List[str]] = Field(None, max_length=256)
    all_clients: Optional[bool] = None
    read_only: Optional[bool] = None
    sftp_access: Optional[SftpAccessValue] = None


class SftpUserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)
    public_keys: List[str] = Field(default_factory=list, max_length=16)
    password: Optional[str] = Field(None, max_length=200)
    generate_password: bool = False
    allowed_ips: List[str] = Field(default_factory=list, max_length=32)


class SftpUserUpdate(BaseModel):
    public_keys: Optional[List[str]] = Field(None, max_length=16)
    password: Optional[str] = Field(None, max_length=200)
    generate_password: bool = False
    clear_password: bool = False
    allowed_ips: Optional[List[str]] = Field(None, max_length=32)
    enabled: Optional[bool] = None


def _raise(exc: StorageError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _share_out(tenant: StorageTenant, share: StorageShare, storage_status: str, *, staff: bool) -> dict:
    endpoint = tenant.endpoint_ip
    out = {
        "id": share.id,
        "name": share.name,
        "quota_bytes": share.quota_bytes,
        "quota_gb": share.quota_bytes // GIB,
        "used_bytes": share.used_bytes,
        "clients": list(share.clients or []),
        "all_clients": bool(share.all_clients),
        "effective_clients": share_clients(tenant, share),
        "read_only": bool(share.read_only),
        "sftp_access": share.sftp_access.value,
        "state": share.state.value,
        "status": share_status(share, storage_status),
        "destroy_after": iso_utc(share.destroy_after),
        "mount_command": f"mount -t nfs4 {endpoint}:/{share.name} /mnt/{share.name}" if endpoint else None,
        "fstab_line": f"{endpoint}:/{share.name} /mnt/{share.name} nfs4 defaults,_netdev 0 0" if endpoint else None,
    }
    if staff:
        out["runner_state"] = share.runner_state
        out["runner_error"] = share.runner_error
    return out


def _tenant_out(db: Session, tenant: StorageTenant, *, staff: bool) -> dict:
    service = tenant.service
    network = tenant.network
    status_value, status_detail = tenant_status(db, tenant)
    active_shares = [s for s in tenant.shares if s.state.value == "active"]
    out = {
        "service_id": tenant.service_id,
        "service_name": service.name if service else None,
        "service_status": service.status.value if service else None,
        "status": status_value,
        "status_detail": status_detail,
        "size_bytes": tenant.size_bytes,
        "size_gb": tenant.size_bytes // GIB,
        "allocated_bytes": sum(s.quota_bytes for s in active_shares),
        "used_bytes": sum(s.used_bytes or 0 for s in active_shares),
        "network": (
            {"id": network.id, "name": network.name, "cidr": network.cidr, "vlan_id": network.segment_id}
            if network is not None
            else None
        ),
        "endpoint_ip": tenant.endpoint_ip,
        "location_id": tenant.location_id,
        "location_name": tenant.location.name if tenant.location is not None else None,
        "terminated_at": iso_utc(tenant.terminated_at),
        "destroy_after": iso_utc(tenant.destroy_after),
        "shares": [_share_out(tenant, s, status_value, staff=staff) for s in tenant.shares],
        "sftp": storage_sftp.tenant_sftp_out(tenant, status_value, staff=staff),
    }
    if staff:
        attachment = storage_attachment(db, tenant)
        node = tenant.node
        owner = service.owner_user if service else None
        out.update(
            tenant_id=tenant.id,
            owner_user_id=service.owner_user_id if service else None,
            owner_username=owner.username if owner else None,
            node={"id": node.id, "name": node.name} if node else None,
            vlan=tenant.vlan,
            network_cidr=tenant.network_cidr,
            attachment_state=attachment.state.value if attachment else None,
            attachment_error=attachment.last_error if attachment else None,
            runner_state=tenant.runner_state,
            runner_error=tenant.runner_error,
            limits=tenant.limits,
        )
    return out


def _tenant(db: Session, service: Service) -> StorageTenant:
    try:
        return StorageService.require_tenant(db, service)
    except StorageError as exc:
        _raise(exc)


async def _link(db: Session, service: Service, body: LinkBody, node_id: Optional[int], *, staff: bool) -> dict:
    try:
        tenant = await StorageService.link_network(
            db,
            service,
            body.network_id,
            node_id=node_id,
            network_name=body.network_name,
            cidr=body.cidr,
            location_id=body.location_id,
        )
    except StorageError as exc:
        _raise(exc)
    return _tenant_out(db, tenant, staff=staff)


def _create_share(db: Session, service: Service, body: ShareCreate, *, staff: bool) -> dict:
    tenant = _tenant(db, service)
    try:
        StorageService.create_share(
            db,
            tenant,
            name=body.name,
            quota_gb=body.quota_gb,
            clients=body.clients,
            all_clients=body.all_clients,
            read_only=body.read_only,
            sftp_access=body.sftp_access,
        )
    except StorageError as exc:
        _raise(exc)
    db.refresh(tenant)
    return _tenant_out(db, tenant, staff=staff)


def _update_share(db: Session, service: Service, share_id: int, body: ShareUpdate, *, staff: bool) -> dict:
    tenant = _tenant(db, service)
    try:
        StorageService.update_share(db, tenant, share_id, **body.model_dump(exclude_unset=True))
    except StorageError as exc:
        _raise(exc)
    db.refresh(tenant)
    return _tenant_out(db, tenant, staff=staff)


def _delete_share(db: Session, service: Service, share_id: int, *, staff: bool) -> dict:
    tenant = _tenant(db, service)
    try:
        StorageService.delete_share(db, tenant, share_id)
    except StorageError as exc:
        _raise(exc)
    db.refresh(tenant)
    return _tenant_out(db, tenant, staff=staff)


def _sftp_raise(exc: storage_sftp.SftpError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _with_password(out: dict, generated: Optional[str]) -> dict:
    if generated is not None:
        out["generated_password"] = generated
    return out


def _create_sftp_user(db: Session, service: Service, body: SftpUserCreate, *, staff: bool) -> dict:
    """Create an SFTP user; a generated password is returned once as ``generated_password``."""
    tenant = _tenant(db, service)
    try:
        _user, generated = storage_sftp.create_user(db, tenant, **body.model_dump())
    except storage_sftp.SftpError as exc:
        _sftp_raise(exc)
    db.refresh(tenant)
    return _with_password(_tenant_out(db, tenant, staff=staff), generated)


def _update_sftp_user(db: Session, service: Service, user_id: int, body: SftpUserUpdate, *, staff: bool) -> dict:
    tenant = _tenant(db, service)
    try:
        _user, generated = storage_sftp.update_user(db, tenant, user_id, **body.model_dump(exclude_unset=True))
    except storage_sftp.SftpError as exc:
        _sftp_raise(exc)
    db.refresh(tenant)
    return _with_password(_tenant_out(db, tenant, staff=staff), generated)


def _delete_sftp_user(db: Session, service: Service, user_id: int, *, staff: bool) -> dict:
    tenant = _tenant(db, service)
    try:
        storage_sftp.delete_user(db, tenant, user_id)
    except storage_sftp.SftpError as exc:
        _sftp_raise(exc)
    db.refresh(tenant)
    return _tenant_out(db, tenant, staff=staff)
