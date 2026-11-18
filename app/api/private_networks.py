"""Private networks API: admin management and the owner-scoped client surface."""
import ipaddress
from datetime import datetime
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_admin
from app.core.database import get_db
from app.dao.location_dao import LocationDAO
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.private_network import PrivateNetwork, PrivateNetworkAttachment, PrivateNetworkPool
from app.models.service import ServiceStatus
from app.services.private_network_service import (
    ATTACHABLE_TYPES,
    DEFAULT_CIDR,
    PrivateNetworkError,
    PrivateNetworkService,
    service_location_id,
    validate_pool_range,
)

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]
UserDep = Annotated[dict, Depends(get_current_user)]

admin_router = APIRouter(prefix="/admin/private-networks", tags=["admin-private-networks"])
client_router = APIRouter(prefix="/client/private-networks", tags=["client"])

_NETWORK_NOT_FOUND = "Private network not found"
_POOL_NOT_FOUND = "Private network pool not found"
_BRIDGE_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,14}$"


# --- schemas ---


class PoolCreate(BaseModel):
    location_id: int
    vlan_start: int
    vlan_end: int
    bridge: str = Field("vmbr0", pattern=_BRIDGE_PATTERN)
    enabled: bool = True
    max_networks_per_client: int = Field(5, ge=0, le=1000)
    max_networks_per_service: int = Field(4, ge=1, le=16)


class PoolUpdate(BaseModel):
    vlan_start: Optional[int] = None
    vlan_end: Optional[int] = None
    bridge: Optional[str] = Field(None, pattern=_BRIDGE_PATTERN)
    enabled: Optional[bool] = None
    max_networks_per_client: Optional[int] = Field(None, ge=0, le=1000)
    max_networks_per_service: Optional[int] = Field(None, ge=1, le=16)


class PoolResponse(BaseModel):
    id: int
    location_id: int
    location_name: Optional[str] = None
    enabled: bool
    vlan_start: int
    vlan_end: int
    bridge: str
    max_networks_per_client: int
    max_networks_per_service: int
    networks_in_use: int
    capacity: int


class AdminNetworkCreate(BaseModel):
    owner_user_id: int
    location_id: int
    name: str = Field(..., min_length=1, max_length=64)
    cidr: Optional[str] = None


class ClientNetworkCreate(BaseModel):
    location_id: int
    name: str = Field(..., min_length=1, max_length=64)
    cidr: Optional[str] = None


class AttachBody(BaseModel):
    service_id: int
    ip_address: Optional[str] = Field(None, max_length=45)


class AttachmentResponse(BaseModel):
    id: int
    service_id: Optional[int] = None
    service_name: Optional[str] = None
    service_type: Optional[str] = None
    ip_address: Optional[str] = None
    prefix_length: int
    mac_address: Optional[str] = None
    state: str
    # Staff only: may name switches and hypervisors.
    last_error: Optional[str] = None
    server_id: Optional[int] = None
    vm_net_index: Optional[int] = None


class NetworkResponse(BaseModel):
    id: int
    name: str
    location_id: int
    location_name: Optional[str] = None
    segmentation_type: str
    vlan_id: int
    cidr: str
    status: str
    created_at: Optional[datetime] = None
    attachments: List[AttachmentResponse]
    # Staff only.
    owner_user_id: Optional[int] = None
    owner_username: Optional[str] = None
    switch_synced: Optional[bool] = None
    last_error: Optional[str] = None


class ClientLocationOption(BaseModel):
    location_id: int
    location_name: str
    max_networks: int
    networks_in_use: int
    max_networks_per_service: int


class ClientServiceOption(BaseModel):
    service_id: int
    name: str
    service_type: str
    location_id: int


class ClientOptionsResponse(BaseModel):
    default_cidr: str
    locations: List[ClientLocationOption]
    services: List[ClientServiceOption]


# --- helpers ---


def _raise(exc: PrivateNetworkError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _pool_out(db: Session, pool: PrivateNetworkPool) -> PoolResponse:
    return PoolResponse(
        id=pool.id,
        location_id=pool.location_id,
        location_name=pool.location.name if pool.location else None,
        enabled=pool.enabled,
        vlan_start=pool.vlan_start,
        vlan_end=pool.vlan_end,
        bridge=pool.bridge,
        max_networks_per_client=pool.max_networks_per_client,
        max_networks_per_service=pool.max_networks_per_service,
        networks_in_use=PrivateNetworkDAO.count_for_location(db, pool.location_id),
        capacity=pool.vlan_end - pool.vlan_start + 1,
    )


def _attachment_out(
    attachment: PrivateNetworkAttachment, prefix_length: int, *, staff: bool
) -> AttachmentResponse:
    service = attachment.service
    return AttachmentResponse(
        id=attachment.id,
        service_id=attachment.service_id,
        service_name=service.name if service else None,
        service_type=service.service_type.value if service else None,
        ip_address=attachment.ip_address,
        prefix_length=prefix_length,
        mac_address=attachment.mac_address,
        state=attachment.state.value,
        last_error=attachment.last_error if staff else None,
        server_id=attachment.server_id if staff else None,
        vm_net_index=attachment.vm_net_index if staff else None,
    )


def _network_out(network: PrivateNetwork, *, staff: bool) -> NetworkResponse:
    prefix_length = ipaddress.ip_network(network.cidr).prefixlen
    owner = network.owner_user if staff else None
    return NetworkResponse(
        id=network.id,
        name=network.name,
        location_id=network.location_id,
        location_name=network.location.name if network.location else None,
        segmentation_type=network.segmentation_type.value,
        vlan_id=network.segment_id,
        cidr=network.cidr,
        status=network.status.value,
        created_at=network.created_at,
        attachments=[_attachment_out(a, prefix_length, staff=staff) for a in network.attachments],
        owner_user_id=network.owner_user_id if staff else None,
        owner_username=owner.username if owner else None,
        switch_synced=network.switch_synced if staff else None,
        last_error=network.last_error if staff else None,
    )


def _admin_network(db: Session, network_id: int) -> PrivateNetwork:
    network = PrivateNetworkDAO.get_network(db, network_id)
    if network is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NETWORK_NOT_FOUND)
    return network


def _attachment_of(network: PrivateNetwork, attachment_id: int) -> PrivateNetworkAttachment:
    for attachment in network.attachments:
        if attachment.id == attachment_id:
            return attachment
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")


async def _attach(db: Session, network: PrivateNetwork, body: AttachBody, *, staff: bool) -> NetworkResponse:
    try:
        await PrivateNetworkService.attach(db, network.id, body.service_id, ip_address=body.ip_address)
    except PrivateNetworkError as exc:
        _raise(exc)
    return _network_out(PrivateNetworkDAO.get_network(db, network.id), staff=staff)


async def _detach(db: Session, network: PrivateNetwork, attachment_id: int, *, staff: bool) -> NetworkResponse:
    attachment = _attachment_of(network, attachment_id)
    try:
        await PrivateNetworkService.detach(db, attachment.id)
    except PrivateNetworkError as exc:
        _raise(exc)
    db.expire_all()
    return _network_out(PrivateNetworkDAO.get_network(db, network.id), staff=staff)


# --- admin: pools ---


@admin_router.get("/pools", response_model=List[PoolResponse])
async def list_pools(auth: AdminDep, db: DbDep):
    return [_pool_out(db, pool) for pool in PrivateNetworkDAO.list_pools(db)]


@admin_router.post("/pools", response_model=PoolResponse, status_code=status.HTTP_201_CREATED)
async def create_pool(data: PoolCreate, auth: AdminDep, db: DbDep):
    if LocationDAO.get_by_id(db, data.location_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    if PrivateNetworkDAO.get_pool_for_location(db, data.location_id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This location already has a pool")
    try:
        validate_pool_range(data.vlan_start, data.vlan_end)
    except PrivateNetworkError as exc:
        _raise(exc)
    return _pool_out(db, PrivateNetworkDAO.create_pool(db, **data.model_dump()))


@admin_router.patch("/pools/{pool_id}", response_model=PoolResponse)
async def update_pool(pool_id: int, data: PoolUpdate, auth: AdminDep, db: DbDep):
    pool = PrivateNetworkDAO.get_pool(db, pool_id)
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_POOL_NOT_FOUND)
    changes = {key: value for key, value in data.model_dump(exclude_unset=True).items() if value is not None}
    try:
        validate_pool_range(
            changes.get("vlan_start", pool.vlan_start), changes.get("vlan_end", pool.vlan_end)
        )
    except PrivateNetworkError as exc:
        _raise(exc)
    for key, value in changes.items():
        setattr(pool, key, value)
    return _pool_out(db, PrivateNetworkDAO.save(db, pool))


@admin_router.delete("/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pool(pool_id: int, auth: AdminDep, db: DbDep):
    pool = PrivateNetworkDAO.get_pool(db, pool_id)
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_POOL_NOT_FOUND)
    if PrivateNetworkDAO.count_for_location(db, pool.location_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Delete the private networks in this location first, or disable the pool instead",
        )
    PrivateNetworkDAO.delete_pool(db, pool)


# --- admin: networks ---


@admin_router.get("", response_model=List[NetworkResponse])
async def admin_list_networks(
    auth: AdminDep,
    db: DbDep,
    owner_user_id: Optional[int] = None,
    location_id: Optional[int] = None,
):
    rows = PrivateNetworkDAO.list_networks(db, owner_user_id=owner_user_id, location_id=location_id)
    return [_network_out(row, staff=True) for row in rows]


@admin_router.post("", response_model=NetworkResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_network(data: AdminNetworkCreate, auth: AdminDep, db: DbDep):
    """Create a network for a client. The per-client limit does not apply to staff."""
    if UserDAO.get_by_id(db, data.owner_user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    try:
        network = await PrivateNetworkService.create_network(
            db,
            owner_user_id=data.owner_user_id,
            location_id=data.location_id,
            name=data.name,
            cidr=data.cidr,
            enforce_limit=False,
        )
    except PrivateNetworkError as exc:
        _raise(exc)
    return _network_out(network, staff=True)


@admin_router.get("/{network_id}", response_model=NetworkResponse)
async def admin_get_network(network_id: int, auth: AdminDep, db: DbDep):
    return _network_out(_admin_network(db, network_id), staff=True)


@admin_router.delete("/{network_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_network(
    network_id: int,
    auth: AdminDep,
    db: DbDep,
    skip_switches: Annotated[
        bool, Query(description="Drop the record without removing the VLAN from the switches")
    ] = False,
):
    _admin_network(db, network_id)
    try:
        await PrivateNetworkService.delete_network(db, network_id, skip_switches=skip_switches)
    except PrivateNetworkError as exc:
        _raise(exc)


@admin_router.post("/{network_id}/sync", response_model=NetworkResponse)
async def admin_sync_network(network_id: int, auth: AdminDep, db: DbDep):
    """Create the network's VLAN again on every managed switch in its location."""
    _admin_network(db, network_id)
    try:
        network = await PrivateNetworkService.sync_network(db, network_id)
    except PrivateNetworkError as exc:
        _raise(exc)
    return _network_out(network, staff=True)


@admin_router.post("/{network_id}/attachments", response_model=NetworkResponse)
async def admin_attach(network_id: int, body: AttachBody, auth: AdminDep, db: DbDep):
    return await _attach(db, _admin_network(db, network_id), body, staff=True)


@admin_router.delete("/{network_id}/attachments/{attachment_id}", response_model=NetworkResponse)
async def admin_detach(network_id: int, attachment_id: int, auth: AdminDep, db: DbDep):
    return await _detach(db, _admin_network(db, network_id), attachment_id, staff=True)


# --- client ---


def _client_user_id(auth: dict) -> int:
    """The signed-in client's user ID. Staff, resellers and API keys have no client side."""
    if auth.get("type") == "api_key" or auth.get("is_admin") or auth.get("is_reseller"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Private networks are managed from a client account"
        )
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return int(user_id)


def _client_network(db: Session, network_id: int, user_id: int) -> PrivateNetwork:
    """A network the caller owns. Someone else's network is reported as missing."""
    network = PrivateNetworkDAO.get_network(db, network_id)
    if network is None or network.owner_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NETWORK_NOT_FOUND)
    return network


@client_router.get("", response_model=List[NetworkResponse])
async def client_list_networks(auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    return [_network_out(row, staff=False) for row in PrivateNetworkDAO.list_networks(db, owner_user_id=user_id)]


@client_router.get("/options", response_model=ClientOptionsResponse)
async def client_options(auth: UserDep, db: DbDep):
    """Where the caller can create networks, and which of their services can join one."""
    user_id = _client_user_id(auth)
    pools = {pool.location_id: pool for pool in PrivateNetworkDAO.list_pools(db) if pool.enabled}
    services = []
    for service in ServiceDAO.get_by_owner_user(db, user_id):
        if service.service_type not in ATTACHABLE_TYPES or service.status != ServiceStatus.ACTIVE:
            continue
        location_id = service_location_id(db, service)
        if location_id in pools:
            services.append(
                ClientServiceOption(
                    service_id=service.id,
                    name=service.name,
                    service_type=service.service_type.value,
                    location_id=location_id,
                )
            )
    # Only locations where the client has something to connect.
    locations = [
        ClientLocationOption(
            location_id=location_id,
            location_name=pool.location.name if pool.location else str(location_id),
            max_networks=pool.max_networks_per_client,
            networks_in_use=PrivateNetworkDAO.count_for_owner(db, user_id, location_id),
            max_networks_per_service=pool.max_networks_per_service,
        )
        for location_id, pool in pools.items()
        if any(option.location_id == location_id for option in services)
    ]
    return ClientOptionsResponse(default_cidr=DEFAULT_CIDR, locations=locations, services=services)


@client_router.post("", response_model=NetworkResponse, status_code=status.HTTP_201_CREATED)
async def client_create_network(data: ClientNetworkCreate, auth: UserDep, db: DbDep):
    user_id = _client_user_id(auth)
    try:
        network = await PrivateNetworkService.create_network(
            db, owner_user_id=user_id, location_id=data.location_id, name=data.name, cidr=data.cidr
        )
    except PrivateNetworkError as exc:
        _raise(exc)
    return _network_out(network, staff=False)


@client_router.get("/{network_id}", response_model=NetworkResponse)
async def client_get_network(network_id: int, auth: UserDep, db: DbDep):
    return _network_out(_client_network(db, network_id, _client_user_id(auth)), staff=False)


@client_router.delete("/{network_id}", status_code=status.HTTP_204_NO_CONTENT)
async def client_delete_network(network_id: int, auth: UserDep, db: DbDep):
    _client_network(db, network_id, _client_user_id(auth))
    try:
        await PrivateNetworkService.delete_network(db, network_id)
    except PrivateNetworkError as exc:
        _raise(exc)


@client_router.post("/{network_id}/attachments", response_model=NetworkResponse)
async def client_attach(network_id: int, body: AttachBody, auth: UserDep, db: DbDep):
    network = _client_network(db, network_id, _client_user_id(auth))
    return await _attach(db, network, body, staff=False)


@client_router.delete("/{network_id}/attachments/{attachment_id}", response_model=NetworkResponse)
async def client_detach(network_id: int, attachment_id: int, auth: UserDep, db: DbDep):
    network = _client_network(db, network_id, _client_user_id(auth))
    return await _detach(db, network, attachment_id, staff=False)
