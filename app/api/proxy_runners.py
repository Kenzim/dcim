"""Admin API for standalone proxy runners."""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.location_dao import LocationDAO
from app.dao.proxy_runner_dao import ProxyRunnerDAO
from app.dao.server_dao import ServerDAO
from app.models.ipam import IPSubnet
from app.models.proxy_runner import ProxyRunner, ProxyRunnerVlanMode
from app.services.proxy_fast_path import ProxyAccessService, port_for_location


from typing import Annotated

_MSG_PROXY_RUNNER_NOT_FOUND = "Proxy runner not found"
DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/admin/proxy-runners", tags=["proxy-runners"])


class ProxyRunnerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class ProxyRunnerUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    enabled: Optional[bool] = None
    # Proxy fast path. Send null to clear location_id or server_id.
    location_id: Optional[int] = None
    server_id: Optional[int] = None
    vlan_mode: Optional[ProxyRunnerVlanMode] = None


class HostedSubnet(BaseModel):
    id: int
    name: str
    cidr: str
    fast_path_ip: Optional[str] = None
    enabled: bool


class ProxyRunnerResponse(BaseModel):
    id: int
    name: str
    enabled: bool
    online: bool
    last_seen_at: Optional[datetime] = None
    last_seen_ip: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    api_key: Optional[str] = None
    location_id: Optional[int] = None
    location_name: Optional[str] = None
    server_id: Optional[int] = None
    server_name: Optional[str] = None
    vlan_mode: ProxyRunnerVlanMode = ProxyRunnerVlanMode.PRECONFIGURED
    fast_path_port: Optional[int] = None
    hosted_subnets: List[HostedSubnet] = []

    class Config:
        from_attributes = True


class ProxyRunnerKeyResponse(BaseModel):
    id: int
    api_key: str


def _hosted_subnets(row: ProxyRunner) -> List[HostedSubnet]:
    db = Session.object_session(row)
    if db is None:
        return []
    subnets = db.query(IPSubnet).filter(IPSubnet.proxy_runner_id == row.id).order_by(IPSubnet.id).all()
    return [
        HostedSubnet(id=s.id, name=s.name, cidr=s.cidr, fast_path_ip=s.fast_path_ip, enabled=s.enabled)
        for s in subnets
    ]


def _to_response(row: ProxyRunner, *, api_key: Optional[str] = None) -> ProxyRunnerResponse:
    return ProxyRunnerResponse(
        location_id=row.location_id,
        location_name=row.location.name if row.location else None,
        server_id=row.server_id,
        server_name=row.server.name if row.server else None,
        vlan_mode=row.vlan_mode or ProxyRunnerVlanMode.PRECONFIGURED,
        fast_path_port=port_for_location(row.location) if row.location_id else None,
        hosted_subnets=_hosted_subnets(row),
        id=row.id,
        name=row.name,
        enabled=row.enabled,
        online=ProxyRunnerDAO.is_online(row),
        last_seen_at=row.last_seen_at,
        last_seen_ip=row.last_seen_ip,
        created_at=row.created_at,
        updated_at=row.updated_at,
        api_key=api_key,
    )


@router.get("", response_model=List[ProxyRunnerResponse])
@router.get("/", response_model=List[ProxyRunnerResponse])
async def list_proxy_runners(
    auth: AdminDep,
    db: DbDep,
):
    return [_to_response(row) for row in ProxyRunnerDAO.get_all(db)]


@router.post("", response_model=ProxyRunnerResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=ProxyRunnerResponse, status_code=status.HTTP_201_CREATED)
async def create_proxy_runner(
    data: ProxyRunnerCreate,
    auth: AdminDep,
    db: DbDep,
):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")
    try:
        row, plaintext = ProxyRunnerDAO.create(db, name=name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return _to_response(row, api_key=plaintext)


@router.get("/{runner_id}", response_model=ProxyRunnerResponse)
async def get_proxy_runner(
    runner_id: int,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxyRunnerDAO.get_by_id(db, runner_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PROXY_RUNNER_NOT_FOUND)
    return _to_response(row)


@router.patch("/{runner_id}", response_model=ProxyRunnerResponse)
async def update_proxy_runner(
    runner_id: int,
    data: ProxyRunnerUpdate,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxyRunnerDAO.get_by_id(db, runner_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PROXY_RUNNER_NOT_FOUND)
    if data.name is not None and not data.name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name cannot be empty")
    placement = data.model_dump(exclude_unset=True).keys() & {"location_id", "server_id", "vlan_mode"}
    if "location_id" in placement and data.location_id is not None:
        if LocationDAO.get_by_id(db, data.location_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    if "server_id" in placement and data.server_id is not None:
        if ServerDAO.get_by_id(db, data.server_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    for key in placement:
        if key == "vlan_mode" and data.vlan_mode is None:
            continue
        setattr(row, key, getattr(data, key))
    if row.vlan_mode == ProxyRunnerVlanMode.SWITCH and row.server_id is None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Switch VLAN mode needs the runner's server"
        )
    row = ProxyRunnerDAO.update(db, row, name=data.name, enabled=data.enabled)
    if placement:
        await ProxyAccessService.sync_runner(db, row.id)
        db.expire_all()
        row = ProxyRunnerDAO.get_by_id(db, runner_id)
    return _to_response(row)


@router.post("/{runner_id}/rotate-key", response_model=ProxyRunnerKeyResponse)
async def rotate_proxy_runner_key(
    runner_id: int,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxyRunnerDAO.get_by_id(db, runner_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PROXY_RUNNER_NOT_FOUND)
    try:
        plaintext = ProxyRunnerDAO.rotate_key(db, row)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return ProxyRunnerKeyResponse(id=row.id, api_key=plaintext)


@router.delete("/{runner_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proxy_runner(
    runner_id: int,
    auth: AdminDep,
    db: DbDep,
):
    if ProxyRunnerDAO.get_by_id(db, runner_id) is not None:
        await ProxyAccessService.release_runner(db, runner_id)
    if not ProxyRunnerDAO.delete(db, runner_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PROXY_RUNNER_NOT_FOUND)
