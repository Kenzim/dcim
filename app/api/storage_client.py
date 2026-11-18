"""NFS storage API for clients: only the caller's own storage, gated by the service's permission preset."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.storage_common import (
    STORAGE_NOT_FOUND,
    DbDep,
    LinkBody,
    ShareCreate,
    ShareUpdate,
    SftpUserCreate,
    SftpUserUpdate,
    _create_sftp_user,
    _create_share,
    _delete_sftp_user,
    _delete_share,
    _link,
    _tenant,
    _tenant_out,
    _update_sftp_user,
    _update_share,
)
from app.core.auth import get_current_user
from app.core.client_permissions import PermissionKey
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.service_dao import ServiceDAO
from app.dao.storage_dao import StorageDAO
from app.models.private_network import NetworkKind, PrivateNetworkStatus
from app.models.service import Service, ServiceStatus, ServiceType
from app.services.client_permission_resolver import require_client_permission
from app.services.storage_service import StorageService

UserDep = Annotated[dict, Depends(get_current_user)]

client_router = APIRouter(prefix="/client/storage", tags=["client"])

__all__ = ["client_router", "LinkBody", "ShareCreate", "ShareUpdate"]


def _client_user_id(auth: dict) -> int:
    """The signed-in client's user ID. Staff, resellers and API keys have no client side."""
    if auth.get("type") == "api_key" or auth.get("is_admin") or auth.get("is_reseller"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Storage is managed from a client account")
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return int(user_id)


def _client_service(db: Session, auth: dict, service_id: int, permission: str) -> Service:
    """A live storage service the caller owns. Anyone else's is reported as missing."""
    user_id = _client_user_id(auth)
    service = ServiceDAO.get_by_id(db, service_id)
    if (
        service is None
        or service.owner_user_id != user_id
        or service.service_type != ServiceType.NFS_STORAGE
        or service.status == ServiceStatus.TERMINATED
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=STORAGE_NOT_FOUND)
    require_client_permission(db, service, permission)
    return service


@client_router.get("")
async def client_list_storage(auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    return [
        _tenant_out(db, tenant, staff=False)
        for tenant in StorageDAO.list_tenants(db, owner_user_id=user_id)
        if tenant.service is not None and tenant.service.status != ServiceStatus.TERMINATED
    ]


@client_router.get("/{service_id}")
async def client_get_storage(service_id: int, auth: UserDep, db: DbDep):
    service = _client_service(db, auth, service_id, PermissionKey.SERVICE_VIEW)
    return _tenant_out(db, _tenant(db, service), staff=False)


@client_router.get("/{service_id}/networks")
async def client_linkable_networks(service_id: int, auth: UserDep, db: DbDep):
    """The caller's networks this storage could be linked to.

    Linking without a network creates one in the storage's location, so a
    client with no network (and no servers) can still use storage.
    """
    service = _client_service(db, auth, service_id, PermissionKey.SERVICE_VIEW)
    tenant = _tenant(db, service)
    node_locations = StorageService.storage_location_ids(db)
    out = []
    for network in PrivateNetworkDAO.list_networks(db, owner_user_id=service.owner_user_id):
        if network.status != PrivateNetworkStatus.ACTIVE or network.kind != NetworkKind.STANDARD:
            continue
        has_storage = StorageDAO.get_tenant_for_network(db, network.id) is not None
        if has_storage:
            reason = "Already has storage"
        elif network.location_id not in node_locations:
            reason = "Storage is not offered in this location"
        elif tenant.location_id is not None and network.location_id != tenant.location_id:
            reason = "This storage was ordered for another location"
        else:
            reason = None
        out.append(
            {
                "id": network.id,
                "name": network.name,
                "cidr": network.cidr,
                "location_id": network.location_id,
                "location_name": network.location.name if network.location else None,
                "available": reason is None,
                "reason": reason,
            }
        )
    return out


@client_router.post("/{service_id}/link")
async def client_link_storage(service_id: int, body: LinkBody, auth: UserDep, db: DbDep):
    service = _client_service(db, auth, service_id, PermissionKey.STORAGE_MANAGE_SHARES)
    return await _link(db, service, body, None, staff=False)


@client_router.post("/{service_id}/shares", status_code=status.HTTP_201_CREATED)
async def client_create_share(service_id: int, body: ShareCreate, auth: UserDep, db: DbDep):
    service = _client_service(db, auth, service_id, PermissionKey.STORAGE_MANAGE_SHARES)
    return _create_share(db, service, body, staff=False)


@client_router.patch("/{service_id}/shares/{share_id}")
async def client_update_share(service_id: int, share_id: int, body: ShareUpdate, auth: UserDep, db: DbDep):
    service = _client_service(db, auth, service_id, PermissionKey.STORAGE_MANAGE_SHARES)
    return _update_share(db, service, share_id, body, staff=False)


@client_router.delete("/{service_id}/shares/{share_id}")
async def client_delete_share(service_id: int, share_id: int, auth: UserDep, db: DbDep):
    service = _client_service(db, auth, service_id, PermissionKey.STORAGE_MANAGE_SHARES)
    return _delete_share(db, service, share_id, staff=False)


@client_router.post("/{service_id}/sftp-users", status_code=status.HTTP_201_CREATED)
async def client_create_sftp_user(service_id: int, body: SftpUserCreate, auth: UserDep, db: DbDep):
    service = _client_service(db, auth, service_id, PermissionKey.STORAGE_MANAGE_SHARES)
    return _create_sftp_user(db, service, body, staff=False)


@client_router.patch("/{service_id}/sftp-users/{user_id}")
async def client_update_sftp_user(service_id: int, user_id: int, body: SftpUserUpdate, auth: UserDep, db: DbDep):
    service = _client_service(db, auth, service_id, PermissionKey.STORAGE_MANAGE_SHARES)
    return _update_sftp_user(db, service, user_id, body, staff=False)


@client_router.delete("/{service_id}/sftp-users/{user_id}")
async def client_delete_sftp_user(service_id: int, user_id: int, auth: UserDep, db: DbDep):
    service = _client_service(db, auth, service_id, PermissionKey.STORAGE_MANAGE_SHARES)
    return _delete_sftp_user(db, service, user_id, staff=False)
