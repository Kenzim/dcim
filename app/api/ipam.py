from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.ipam_dao import IPAMDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ServiceType


router = APIRouter(prefix="/ipam", tags=["ipam"])


class SubnetCreate(BaseModel):
    name: str
    cidr: str
    location_id: Optional[int] = None
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    allocation_strategy: str = "first_free"


class AssignIPRequest(BaseModel):
    service_id: int
    subnet_id: Optional[int] = None
    strategy: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    assigned_by: Optional[str] = None


@router.get("/subnets")
async def list_subnets(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    subnets = IPAMDAO.list_subnets(db)
    return [
        {
            "id": s.id,
            "name": s.name,
            "cidr": s.cidr,
            "location_id": s.location_id,
            "range_start": s.range_start,
            "range_end": s.range_end,
            "tags": s.tags,
            "allocation_strategy": s.allocation_strategy,
            "enabled": s.enabled,
            "total_ips": len(s.ip_addresses or []),
            "assigned_ips": len([ip for ip in (s.ip_addresses or []) if ip.state == "assigned"]),
        }
        for s in subnets
    ]


@router.post("/subnets", status_code=status.HTTP_201_CREATED)
async def create_subnet(
    data: SubnetCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = IPAMDAO.create_subnet(db, **data.model_dump())
    return {"id": row.id}


@router.post("/assignments", status_code=status.HTTP_201_CREATED)
async def assign_ip(
    data: AssignIPRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = ServiceDAO.get_by_id(db, data.service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.HTTP_PROXY:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="IP assignments are only allowed for http_proxy services")
    try:
        assignment = IPAMDAO.assign_ip(db, **data.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {
        "id": assignment.id,
        "service_id": assignment.service_id,
        "ip_address": assignment.ip.ip_address if assignment.ip else None,
        "username": assignment.username,
        "password": assignment.password,
    }


@router.delete("/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def release_ip(
    assignment_id: int,
    released_by: Optional[str] = None,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ok = IPAMDAO.release_ip(db, assignment_id=assignment_id, released_by=released_by)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    return None


@router.get("/services/{service_id}/assignments")
async def list_service_assignments(
    service_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    assignments = IPAMDAO.get_assignment_by_service(db, service_id=service_id)
    return [
        {
            "id": a.id,
            "service_id": a.service_id,
            "ip_address": a.ip.ip_address if a.ip else None,
            "username": a.username,
            "password": a.password,
            "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
        }
        for a in assignments
    ]


@router.get("/history")
async def list_history(
    service_id: Optional[int] = None,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = IPAMDAO.list_history(db, service_id=service_id)
    return [
        {
            "id": r.id,
            "service_id": r.service_id,
            "ip_address": r.ip_address,
            "subnet_cidr": r.subnet_cidr,
            "action": r.action,
            "username": r.username,
            "assigned_by": r.assigned_by,
            "details": r.details,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
