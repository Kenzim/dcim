"""Admin API: patch panels. Cables on panel ports are created through /api/cable-runs with
port type 'patch' and a side ('front' or 'rear')."""
from datetime import datetime
from typing import Annotated, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.patch_panel_dao import PatchPanelDAO
from app.services import patch_panel_service as svc

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/patch-panels", tags=["patch-panels"])

Medium = Literal["copper", "fibre"]


class PatchPanelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    location_id: int
    rack_id: Optional[int] = None
    rack_unit: Optional[int] = None
    rack_units: int = 1
    port_count: int
    medium: Medium = "copper"
    description: Optional[str] = None


class PatchPanelUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    location_id: Optional[int] = None
    rack_id: Optional[int] = None
    rack_unit: Optional[int] = None
    rack_units: Optional[int] = None
    port_count: Optional[int] = None
    medium: Optional[Medium] = None
    description: Optional[str] = None


class PatchPortUpdate(BaseModel):
    label: Optional[str] = Field(None, max_length=255)


class PortEnd(BaseModel):
    type: str
    id: int
    side: Optional[str] = None
    port_name: Optional[str] = None
    device_id: Optional[int] = None
    device_name: Optional[str] = None


class PortSide(BaseModel):
    cable_id: int
    cable_type: Optional[str] = None
    speed_mbps: Optional[int] = None
    description: Optional[str] = None
    other_end: Optional[PortEnd] = None
    reaches: Optional[PortEnd] = None


class PatchPortResponse(BaseModel):
    id: int
    number: int
    label: Optional[str] = None
    name: str
    front: Optional[PortSide] = None
    rear: Optional[PortSide] = None


class PatchPanelResponse(BaseModel):
    id: int
    name: str
    location_id: int
    rack_id: Optional[int] = None
    rack_unit: Optional[int] = None
    rack_units: int
    port_count: int
    medium: str
    description: Optional[str] = None
    cable_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PatchPanelDetail(PatchPanelResponse):
    ports: List[PatchPortResponse]


def _summary(db: Session, panel) -> dict:
    return {
        "id": panel.id,
        "name": panel.name,
        "location_id": panel.location_id,
        "rack_id": panel.rack_id,
        "rack_unit": panel.rack_unit,
        "rack_units": panel.rack_units or 1,
        "port_count": panel.port_count,
        "medium": panel.medium,
        "description": panel.description,
        "cable_count": svc.cable_count(db, panel.id),
        "created_at": panel.created_at,
        "updated_at": panel.updated_at,
    }


def _detail(db: Session, panel) -> dict:
    return {**_summary(db, panel), "ports": svc.port_views(db, panel)}


def _get(db: Session, panel_id: int):
    panel = PatchPanelDAO.get_by_id(db, panel_id)
    if panel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patch panel not found")
    return panel


@router.get("/", response_model=List[PatchPanelResponse])
def list_patch_panels(
    auth: AdminDep,
    db: DbDep,
    location_id: Optional[int] = Query(None, ge=1),
    rack_id: Optional[int] = Query(None, ge=1),
):
    return [_summary(db, p) for p in PatchPanelDAO.list(db, location_id=location_id, rack_id=rack_id)]


@router.post("/", response_model=PatchPanelDetail, status_code=status.HTTP_201_CREATED)
def create_patch_panel(data: PatchPanelCreate, auth: AdminDep, db: DbDep):
    name = data.name.strip()
    try:
        svc.validate_placement(db, data.location_id, data.rack_id, data.rack_unit, data.rack_units, data.port_count, data.medium)
        svc.ensure_unique_name(db, data.location_id, name)
    except svc.PatchPanelError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    panel = PatchPanelDAO.create(
        db,
        port_count=data.port_count,
        name=name,
        location_id=data.location_id,
        rack_id=data.rack_id,
        rack_unit=data.rack_unit,
        rack_units=data.rack_units,
        medium=data.medium,
        description=data.description,
    )
    return _detail(db, PatchPanelDAO.get_by_id(db, panel.id))


@router.get("/{panel_id}", response_model=PatchPanelDetail)
def get_patch_panel(panel_id: int, auth: AdminDep, db: DbDep):
    return _detail(db, _get(db, panel_id))


@router.put("/{panel_id}", response_model=PatchPanelDetail)
def update_patch_panel(panel_id: int, data: PatchPanelUpdate, auth: AdminDep, db: DbDep):
    panel = _get(db, panel_id)
    fields = data.model_dump(exclude_unset=True)
    location_id = fields.get("location_id", panel.location_id)
    rack_id = fields["rack_id"] if "rack_id" in fields else panel.rack_id
    rack_unit = fields["rack_unit"] if "rack_unit" in fields else panel.rack_unit
    if rack_id is None:
        rack_unit = None
    rack_units = fields.get("rack_units") or panel.rack_units or 1
    port_count = fields.get("port_count") or panel.port_count
    medium = fields.get("medium") or panel.medium
    name = (fields.get("name") or panel.name).strip()
    try:
        svc.validate_placement(db, location_id, rack_id, rack_unit, rack_units, port_count, medium, exclude_panel_id=panel.id)
        svc.ensure_unique_name(db, location_id, name, exclude_panel_id=panel.id)
    except svc.PatchPanelError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if port_count < panel.port_count:
        removed = {p.id for p in panel.ports if p.number > port_count}
        from app.dao.cable_run_dao import CableRunDAO
        if any(
            c.end_a_patch_port_id in removed or c.end_b_patch_port_id in removed
            for c in CableRunDAO.get_by_patch_panel(db, panel.id)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ports above {port_count} have cables; disconnect them before reducing the port count",
            )
    panel.name = name
    panel.location_id = location_id
    panel.rack_id = rack_id
    panel.rack_unit = rack_unit
    panel.rack_units = rack_units
    panel.medium = medium
    if "description" in fields:
        panel.description = fields["description"]
    if port_count != panel.port_count:
        PatchPanelDAO.resize(db, panel, port_count)
    PatchPanelDAO.save(db, panel)
    return _detail(db, _get(db, panel_id))


@router.put("/{panel_id}/ports/{number}", response_model=PatchPanelDetail)
def update_patch_port(panel_id: int, number: int, data: PatchPortUpdate, auth: AdminDep, db: DbDep):
    panel = _get(db, panel_id)
    port = PatchPanelDAO.get_port_by_number(db, panel.id, number)
    if port is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patch panel port not found")
    port.label = (data.label or "").strip() or None
    PatchPanelDAO.save(db, panel)
    return _detail(db, _get(db, panel_id))


@router.delete("/{panel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_patch_panel(panel_id: int, auth: AdminDep, db: DbDep, force: bool = False):
    """Delete a panel. Refused while it has cables unless force=true, which deletes them too."""
    panel = _get(db, panel_id)
    cables = svc.cable_count(db, panel.id)
    if cables and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Patch panel has {cables} cable(s); delete with force=true to remove them too",
        )
    if cables:
        from app.dao.cable_run_dao import CableRunDAO
        for c in CableRunDAO.get_by_patch_panel(db, panel.id):
            db.delete(c)
        db.flush()
    PatchPanelDAO.delete(db, panel)
