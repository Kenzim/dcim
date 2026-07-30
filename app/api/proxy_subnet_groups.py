"""Admin API for proxy IPAM subnet groups (pools)."""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.proxy_subnet_group_dao import ProxySubnetGroupDAO
from app.models.proxy_subnet_group import ProxySubnetGroup


router = APIRouter(prefix="/admin/proxy-subnet-groups", tags=["proxy-subnet-groups"])


class ProxySubnetGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    code: Optional[str] = Field(None, max_length=128)
    description: Optional[str] = None
    enabled: bool = True
    subnet_ids: List[int] = Field(default_factory=list)


class ProxySubnetGroupUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: Optional[bool] = None
    subnet_ids: Optional[List[int]] = None


class ProxySubnetGroupMemberOut(BaseModel):
    subnet_id: int
    name: str
    cidr: str
    enabled: bool
    free_ips: Optional[int] = None
    total_ips: Optional[int] = None


class ProxySubnetGroupResponse(BaseModel):
    id: int
    name: str
    code: str
    description: Optional[str] = None
    enabled: bool
    subnet_ids: List[int]
    members: List[ProxySubnetGroupMemberOut]
    created_at: datetime
    updated_at: datetime


def _member_out(subnet) -> ProxySubnetGroupMemberOut:
    free = None
    total = None
    try:
        # Prefer counts already exposed by IPAM list helpers when present.
        free = getattr(subnet, "free_ips", None)
        total = getattr(subnet, "total_ips", None)
        if free is None and hasattr(subnet, "ip_addresses"):
            ips = list(subnet.ip_addresses or [])
            total = len(ips)
            free = sum(1 for ip in ips if getattr(ip, "state", None) == "free")
    except Exception:
        pass
    return ProxySubnetGroupMemberOut(
        subnet_id=subnet.id,
        name=subnet.name,
        cidr=subnet.cidr,
        enabled=bool(subnet.enabled),
        free_ips=free,
        total_ips=total,
    )


def _to_response(row: ProxySubnetGroup) -> ProxySubnetGroupResponse:
    members = []
    subnet_ids = []
    for m in row.members or []:
        subnet_ids.append(m.subnet_id)
        if m.subnet is not None:
            members.append(_member_out(m.subnet))
    return ProxySubnetGroupResponse(
        id=row.id,
        name=row.name,
        code=row.code,
        description=row.description,
        enabled=row.enabled,
        subnet_ids=subnet_ids,
        members=members,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=List[ProxySubnetGroupResponse])
@router.get("/", response_model=List[ProxySubnetGroupResponse])
async def list_proxy_subnet_groups(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return [_to_response(row) for row in ProxySubnetGroupDAO.list_all(db)]


@router.post("", response_model=ProxySubnetGroupResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=ProxySubnetGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_proxy_subnet_group(
    data: ProxySubnetGroupCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        row = ProxySubnetGroupDAO.create(
            db,
            name=data.name,
            code=data.code,
            description=data.description,
            enabled=data.enabled,
            subnet_ids=data.subnet_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_response(row)


@router.get("/{group_id}", response_model=ProxySubnetGroupResponse)
async def get_proxy_subnet_group(
    group_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProxySubnetGroupDAO.get_by_id(db, group_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proxy subnet group not found")
    return _to_response(row)


@router.patch("/{group_id}", response_model=ProxySubnetGroupResponse)
async def update_proxy_subnet_group(
    group_id: int,
    data: ProxySubnetGroupUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProxySubnetGroupDAO.get_by_id(db, group_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proxy subnet group not found")
    try:
        row = ProxySubnetGroupDAO.update(
            db,
            row,
            name=data.name,
            description=data.description,
            enabled=data.enabled,
            subnet_ids=data.subnet_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_response(row)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proxy_subnet_group(
    group_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not ProxySubnetGroupDAO.delete(db, group_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proxy subnet group not found")
