"""NFS storage API for staff: storage nodes and nfs_storage services with their shares."""

from __future__ import annotations

from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
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
    _raise,
    _tenant,
    _tenant_out,
    _update_sftp_user,
    _update_share,
)
from app.core.auth import require_admin
from app.dao.network_port_dao import NetworkPortDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.storage_dao import StorageDAO
from app.dao.user_dao import UserDAO
from app.models.service import Service, ServiceType
from app.models.storage import StorageNode
from app.services.private_network_service import preview_uplink_ports
from app.services.provisioning import (
    ProvisioningActor,
    ProvisioningError,
    ProvisioningService,
    ProvisionRequest,
    infer_provisioning_source,
)
from app.services.storage_service import StorageError, StorageNodeService, StorageService, build_runner_config, iso_utc

AdminDep = Annotated[dict, Depends(require_admin)]

admin_router = APIRouter(prefix="/admin/storage", tags=["admin-storage"])

__all__ = ["admin_router", "LinkBody", "ShareCreate", "ShareUpdate"]


class NodeCreate(BaseModel):
    server_id: int
    name: Optional[str] = Field(None, max_length=64)
    vlan_mode: str = "switch"
    vlan_port_ids: List[int] = Field(default_factory=list, max_length=16)
    enabled: bool = True


class NodeUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=64)
    enabled: Optional[bool] = None
    vlan_mode: Optional[str] = None
    vlan_port_ids: Optional[List[int]] = Field(None, max_length=16)
    sftp_enabled: Optional[bool] = None
    sftp_host: Optional[str] = Field(None, max_length=253)
    sftp_port: Optional[int] = Field(None, ge=1, le=65535)


class StorageCreate(BaseModel):
    owner_user_id: int
    name: str = Field(..., min_length=1, max_length=255)
    size_gb: int = Field(..., ge=1)
    product_code: Optional[str] = None
    description: Optional[str] = None
    network_id: Optional[int] = None
    node_id: Optional[int] = None
    location_id: Optional[int] = None


class AdminLinkBody(LinkBody):
    node_id: Optional[int] = None


class ResizeBody(BaseModel):
    size_gb: int = Field(..., ge=1)


class LimitsBody(BaseModel):
    bandwidth_mbps: Optional[int] = Field(None, ge=1)
    iops: Optional[int] = Field(None, ge=1)


def _node_out(db: Session, node: StorageNode, counts: dict, *, detail: bool = False) -> dict:
    server = node.server
    location = server.location if server is not None else None
    out = {
        "id": node.id,
        "name": node.name,
        "server_id": node.server_id,
        "server_name": server.name if server else None,
        "location_id": server.location_id if server else None,
        "location_name": location.name if location else None,
        "enabled": node.enabled,
        "vlan_mode": node.vlan_mode.value,
        "vlan_port_ids": list(node.vlan_port_ids or []),
        "online": StorageDAO.is_online(node),
        "last_seen_at": iso_utc(node.last_seen_at),
        "last_seen_ip": node.last_seen_ip,
        "runner_version": node.runner_version,
        "ceph_ok": node.ceph_ok,
        "free_bytes": node.free_bytes,
        "last_error": node.last_error,
        "version_applied": node.version_applied,
        "orphans": list(node.orphans or []),
        "api_key_prefix": node.api_key_prefix,
        "live_tenants": counts.get(node.id, 0),
        "sftp_enabled": bool(node.sftp_enabled),
        "sftp_host": node.sftp_host,
        "sftp_port": node.sftp_port,
        "sftp_listening": node.sftp_listening,
        "sftp_host_key_fingerprint": node.sftp_host_key_fingerprint,
        "sftp_error": node.sftp_error,
    }
    if detail:
        out["config_version"] = build_runner_config(db, node)["version"]
        out["in_sync"] = out["config_version"] == node.version_applied
        out["vlan_ports"] = preview_uplink_ports(db, node.server_id)
        out["server_ports"] = _server_ports(db, node.server_id)
        out["tenants"] = [_tenant_out(db, t, staff=True) for t in StorageDAO.tenants_for_node(db, node.id)]
    return out


def _server_ports(db: Session, server_id: int) -> List[dict]:
    return [
        {
            "id": port.id,
            "name": port.name,
            "speed_mbps": port.speed_mbps,
            "pxe": bool(port.pxe_boot),
            "lag_group": port.lag_group,
        }
        for port in NetworkPortDAO.get_by_server(db, server_id)
    ]


def _admin_service(db: Session, service_id: int) -> Service:
    service = ServiceDAO.get_by_id(db, service_id)
    if service is None or service.service_type != ServiceType.NFS_STORAGE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=STORAGE_NOT_FOUND)
    return service


@admin_router.get("/nodes")
async def list_nodes(auth: AdminDep, db: DbDep, server_id: Optional[int] = None):
    counts = StorageDAO.live_tenant_counts(db)
    return [
        _node_out(db, node, counts, detail=server_id is not None)
        for node in StorageDAO.list_nodes(db)
        if server_id is None or node.server_id == server_id
    ]


@admin_router.post("/nodes", status_code=status.HTTP_201_CREATED)
async def create_node(data: NodeCreate, auth: AdminDep, db: DbDep):
    """Make a server a storage node. The runner API key is returned once."""
    try:
        node, api_key = StorageNodeService.create(db, **data.model_dump())
    except StorageError as exc:
        _raise(exc)
    out = _node_out(db, node, StorageDAO.live_tenant_counts(db), detail=True)
    out["api_key"] = api_key
    return out


@admin_router.get("/nodes/{node_id}")
async def get_node(node_id: int, auth: AdminDep, db: DbDep):
    try:
        node = StorageNodeService.require(db, node_id)
    except StorageError as exc:
        _raise(exc)
    return _node_out(db, node, StorageDAO.live_tenant_counts(db), detail=True)


@admin_router.patch("/nodes/{node_id}")
async def update_node(node_id: int, data: NodeUpdate, auth: AdminDep, db: DbDep):
    try:
        node = StorageNodeService.update(db, node_id, **data.model_dump(exclude_unset=True))
    except StorageError as exc:
        _raise(exc)
    return _node_out(db, node, StorageDAO.live_tenant_counts(db), detail=True)


@admin_router.post("/nodes/{node_id}/rotate-key")
async def rotate_node_key(node_id: int, auth: AdminDep, db: DbDep):
    """Issue a new runner API key; the old one stops working at once."""
    try:
        node, api_key = StorageNodeService.rotate_key(db, node_id)
    except StorageError as exc:
        _raise(exc)
    return {"id": node.id, "api_key": api_key, "api_key_prefix": node.api_key_prefix}


@admin_router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(node_id: int, auth: AdminDep, db: DbDep):
    try:
        StorageNodeService.delete(db, node_id)
    except StorageError as exc:
        _raise(exc)


@admin_router.get("/servers/{server_id}/vlan-ports")
async def server_vlan_ports(server_id: int, auth: AdminDep, db: DbDep):
    """The server's ports, and which switch ports would carry customer VLANs."""
    if ServerDAO.get_by_id(db, server_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    return {"server_ports": _server_ports(db, server_id), "vlan_ports": preview_uplink_ports(db, server_id)}


@admin_router.get("/services")
async def admin_list_storage(auth: AdminDep, db: DbDep, owner_user_id: Optional[int] = None):
    return [_tenant_out(db, t, staff=True) for t in StorageDAO.list_tenants(db, owner_user_id=owner_user_id)]


@admin_router.post("/services", status_code=status.HTTP_201_CREATED)
async def admin_create_storage(data: StorageCreate, auth: AdminDep, db: DbDep):
    """Create an nfs_storage service for a client, optionally linking it to one of their networks."""
    if UserDAO.get_by_id(db, data.owner_user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    actor = ProvisioningActor(
        kind="admin", actor_id=int(auth.get("user_id") or 0), name=str(auth.get("username") or "admin"),
        source="admin_api",
    )
    try:
        req = ProvisionRequest(
            name=data.name,
            service_type=ServiceType.NFS_STORAGE,
            owner_user_id=data.owner_user_id,
            product_code=data.product_code,
            description=data.description,
            provisioning_source=infer_provisioning_source(db, data.owner_user_id),
            size_gb=data.size_gb,
            location_id=data.location_id,
        )
        service = ProvisioningService.create(db, req, actor)
    except ProvisioningError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    if data.network_id is not None:
        return await _link(db, service, LinkBody(network_id=data.network_id), data.node_id, staff=True)
    return _tenant_out(db, _tenant(db, service), staff=True)


@admin_router.get("/services/{service_id}")
async def admin_get_storage(service_id: int, auth: AdminDep, db: DbDep):
    return _tenant_out(db, _tenant(db, _admin_service(db, service_id)), staff=True)


@admin_router.post("/services/{service_id}/link")
async def admin_link_storage(service_id: int, body: AdminLinkBody, auth: AdminDep, db: DbDep):
    return await _link(db, _admin_service(db, service_id), body, body.node_id, staff=True)


@admin_router.post("/services/{service_id}/resize")
async def admin_resize_storage(service_id: int, body: ResizeBody, auth: AdminDep, db: DbDep):
    try:
        tenant = StorageService.resize(db, _admin_service(db, service_id), body.size_gb)
    except StorageError as exc:
        _raise(exc)
    return _tenant_out(db, tenant, staff=True)


@admin_router.put("/services/{service_id}/limits")
async def admin_set_limits(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
    body: Optional[LimitsBody] = None,
):
    """Store bandwidth/IOPS limits for the runner (send no body to clear). Not enforced yet."""
    try:
        tenant = StorageService.set_limits(
            db, _admin_service(db, service_id), body.model_dump() if body is not None else None
        )
    except StorageError as exc:
        _raise(exc)
    return _tenant_out(db, tenant, staff=True)


@admin_router.post("/services/{service_id}/shares", status_code=status.HTTP_201_CREATED)
async def admin_create_share(service_id: int, body: ShareCreate, auth: AdminDep, db: DbDep):
    return _create_share(db, _admin_service(db, service_id), body, staff=True)


@admin_router.patch("/services/{service_id}/shares/{share_id}")
async def admin_update_share(service_id: int, share_id: int, body: ShareUpdate, auth: AdminDep, db: DbDep):
    return _update_share(db, _admin_service(db, service_id), share_id, body, staff=True)


@admin_router.delete("/services/{service_id}/shares/{share_id}")
async def admin_delete_share(service_id: int, share_id: int, auth: AdminDep, db: DbDep):
    return _delete_share(db, _admin_service(db, service_id), share_id, staff=True)


@admin_router.post("/services/{service_id}/sftp-users", status_code=status.HTTP_201_CREATED)
async def admin_create_sftp_user(service_id: int, body: SftpUserCreate, auth: AdminDep, db: DbDep):
    return _create_sftp_user(db, _admin_service(db, service_id), body, staff=True)


@admin_router.patch("/services/{service_id}/sftp-users/{user_id}")
async def admin_update_sftp_user(service_id: int, user_id: int, body: SftpUserUpdate, auth: AdminDep, db: DbDep):
    return _update_sftp_user(db, _admin_service(db, service_id), user_id, body, staff=True)


@admin_router.delete("/services/{service_id}/sftp-users/{user_id}")
async def admin_delete_sftp_user(service_id: int, user_id: int, auth: AdminDep, db: DbDep):
    return _delete_sftp_user(db, _admin_service(db, service_id), user_id, staff=True)
