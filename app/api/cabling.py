"""Admin API: read-only cabling graph for the Cabling visualisation page."""
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.services.cabling_graph_service import build_cabling_graph

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/admin/cabling", tags=["cabling"])


class CablingFilters(BaseModel):
    location_id: Optional[int] = None
    rack_id: Optional[int] = None


class CablingLocation(BaseModel):
    id: int
    name: str


class CablingRack(BaseModel):
    id: int
    name: str
    location_id: int
    row: Optional[int] = None
    row_position: Optional[int] = None
    units: Optional[int] = None


class CablingNode(BaseModel):
    id: str
    kind: str
    device_id: int
    name: str
    ip: Optional[str] = None
    model: Optional[str] = None
    location_id: int
    rack_id: Optional[int] = None
    rack_unit: Optional[int] = None
    rack_units: Optional[int] = None
    enabled: bool
    locked: bool
    in_scope: bool
    port_count: int


class CablingPort(BaseModel):
    id: str
    kind: str
    port_id: int
    device: str
    name: str
    mac_address: Optional[str] = None
    speed_mbps: Optional[int] = None
    is_physical: bool
    lag_group: Optional[str] = None
    pxe_boot: bool
    if_index: Optional[int] = None
    admin_status: Optional[str] = None
    oper_status: Optional[str] = None
    description: Optional[str] = None
    cable_id: Optional[int] = None


class CablingEdge(BaseModel):
    id: str
    cable_id: int
    source: str
    target: str
    source_port: str
    target_port: str
    cable_type: Optional[str] = None
    speed_mbps: Optional[int] = None
    effective_speed_mbps: Optional[int] = None
    link_status: str
    description: Optional[str] = None


class CablingGraphResponse(BaseModel):
    filters: CablingFilters
    locations: List[CablingLocation]
    racks: List[CablingRack]
    nodes: List[CablingNode]
    ports: List[CablingPort]
    edges: List[CablingEdge]


@router.get("/graph", response_model=CablingGraphResponse)
def get_cabling_graph(
    auth: AdminDep,
    db: DbDep,
    location_id: Optional[int] = Query(None, ge=1),
    rack_id: Optional[int] = Query(None, ge=1),
):
    """Servers, switches, their ports and the cable runs between them, optionally filtered by location and rack."""
    return build_cabling_graph(db, location_id=location_id, rack_id=rack_id)
