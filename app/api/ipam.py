from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.ipam_dao import IPAMDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ServiceType
from app.services.proxy_credentials import generate_proxy_password, generate_proxy_username


router = APIRouter(prefix="/ipam", tags=["ipam"])


class SubnetCreate(BaseModel):
    name: str
    cidr: str
    location_id: Optional[int] = None
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    allocation_strategy: str = "first_free"
    max_resale_count: int = 1


class SubnetUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    allocation_strategy: Optional[str] = None
    location_id: Optional[int] = None
    clear_location: bool = False
    max_resale_count: Optional[int] = None


class AssignIPRequest(BaseModel):
    service_id: int
    subnet_id: Optional[int] = None
    strategy: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    assigned_by: Optional[str] = None


class RotateCredentialsRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    rotated_by: Optional[str] = None


def _subnet_payload(s, db: Optional[Session] = None) -> dict:
    total_ips = len(s.ip_addresses or [])
    max_resale = s.max_resale_count or 1
    total_slots = total_ips * max_resale
    used_slots = IPAMDAO.used_slots(db, s.id) if db is not None else None
    return {
        "id": s.id,
        "name": s.name,
        "cidr": s.cidr,
        "location_id": s.location_id,
        "range_start": s.range_start,
        "range_end": s.range_end,
        "tags": s.tags,
        "allocation_strategy": s.allocation_strategy,
        "enabled": s.enabled,
        "max_resale_count": max_resale,
        "total_ips": total_ips,
        "assigned_ips": len([ip for ip in (s.ip_addresses or []) if ip.state == "assigned"]),
        "total_slots": total_slots,
        "used_slots": used_slots,
        "free_slots": (total_slots - used_slots) if used_slots is not None else None,
    }


@router.get("/subnets")
async def list_subnets(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    subnets = IPAMDAO.list_subnets(db)
    return [_subnet_payload(s, db) for s in subnets]


@router.post("/subnets", status_code=status.HTTP_201_CREATED)
async def create_subnet(
    data: SubnetCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        row = IPAMDAO.create_subnet(db, **data.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"id": row.id}


@router.patch("/subnets/{subnet_id}")
async def update_subnet(
    subnet_id: int,
    data: SubnetUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        row = IPAMDAO.update_subnet(
            db,
            subnet_id,
            name=data.name,
            enabled=data.enabled,
            allocation_strategy=data.allocation_strategy,
            location_id=data.location_id,
            clear_location=data.clear_location,
            max_resale_count=data.max_resale_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")
    return _subnet_payload(row, db)


@router.delete("/subnets/{subnet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subnet(
    subnet_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        IPAMDAO.delete_subnet(db, subnet_id)
    except ValueError as exc:
        detail = str(exc)
        code = status.HTTP_404_NOT_FOUND if detail == "Subnet not found" else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code, detail=detail)
    return None


@router.get("/assignments")
async def list_assignments(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    assignments = IPAMDAO.list_assignments(db)
    rows = []
    for a in assignments:
        ip_row = a.ip
        subnet = ip_row.subnet if ip_row else None
        service = a.service
        rows.append(
            {
                "id": a.id,
                "service_id": a.service_id,
                "service_name": service.name if service else None,
                "ip_address": ip_row.ip_address if ip_row else None,
                "subnet_id": subnet.id if subnet else None,
                "subnet_cidr": subnet.cidr if subnet else None,
                "username": a.username,
                "password": a.password,
                "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
                "assigned_by": a.assigned_by,
            }
        )
    return rows


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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="IP assignments are only allowed for http_proxy services",
        )
    payload = data.model_dump()
    if not payload.get("username"):
        payload["username"] = generate_proxy_username()
    if not payload.get("password"):
        payload["password"] = generate_proxy_password()
    try:
        assignment = IPAMDAO.assign_ip(db, **payload)
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


@router.post("/assignments/{assignment_id}/rotate")
async def rotate_assignment_credentials(
    assignment_id: int,
    data: RotateCredentialsRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Generate fresh username/password for an existing assignment (same IP)."""
    username = data.username or generate_proxy_username()
    password = data.password or generate_proxy_password()
    assignment = IPAMDAO.rotate_credentials(
        db,
        assignment_id=assignment_id,
        username=username,
        password=password,
        rotated_by=data.rotated_by or "admin",
    )
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    return {
        "id": assignment.id,
        "service_id": assignment.service_id,
        "ip_address": assignment.ip.ip_address if assignment.ip else None,
        "username": assignment.username,
        "password": assignment.password,
    }


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
