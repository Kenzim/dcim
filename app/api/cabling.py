"""Admin API: read-only cabling graph for the Cabling visualisation page."""
from datetime import datetime
from typing import Annotated, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.network_switch_dao import NetworkSwitchDAO
from app.services.cabling_graph_service import build_cabling_graph
from app.services.lldp_collection_service import LldpNotSupported, collect_for_switch
from app.services.lldp_compare_service import compare_lldp

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
    status_updated_at: Optional[datetime] = None
    status_fresh: bool = False
    link_up: Optional[bool] = None
    link_checked_at: Optional[datetime] = None
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
    link_status: Literal["up", "down", "stale", "unknown"]
    description: Optional[str] = None


class CablingGraphResponse(BaseModel):
    filters: CablingFilters
    generated_at: datetime
    status_stale_after_seconds: int
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


class LldpRefreshResponse(BaseModel):
    switch_id: int
    neighbours: int
    matched_ports: int
    collected_at: Optional[datetime] = None


@router.post("/lldp/switches/{switch_id}/refresh", response_model=LldpRefreshResponse)
async def refresh_switch_lldp(switch_id: int, auth: AdminDep, db: DbDep):
    """Read the switch's LLDP neighbour table now (read-only SNMP walk) and replace the stored snapshot."""
    switch = NetworkSwitchDAO.get_by_id(db, switch_id)
    if switch is None:
        raise HTTPException(status_code=404, detail="Switch not found")
    try:
        result = await collect_for_switch(db, switch)
    except LldpNotSupported as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLDP collection failed: {e}")
    db.refresh(switch)
    return {**result, "collected_at": switch.lldp_collected_at}


class LldpEndpoint(BaseModel):
    kind: Literal["server", "switch"]
    device_id: int
    device: str
    device_name: Optional[str] = None
    port_id: Optional[int] = None
    port: Optional[str] = None
    port_name: Optional[str] = None


class LldpObserved(BaseModel):
    chassis_id: Optional[str] = None
    port_id: Optional[str] = None
    port_desc: Optional[str] = None
    sys_name: Optional[str] = None
    resolved: Optional[LldpEndpoint] = None
    resolved_by: Optional[Literal["port_mac", "chassis_mac", "system_name"]] = None


class LldpResult(BaseModel):
    switch_id: int
    switch: str
    switch_port_id: Optional[int] = None
    switch_port: Optional[str] = None
    switch_port_name: Optional[str] = None
    local_port_label: Optional[str] = None
    status: Literal["match", "mismatch", "missing_cable", "unknown_neighbour", "no_lldp", "unmapped_local_port"]
    cable_id: Optional[int] = None
    recorded: Optional[LldpEndpoint] = None
    observed: Optional[LldpObserved] = None


class LldpSwitchStatus(BaseModel):
    id: int
    name: str
    device: str
    supports_lldp: bool
    lldp_collected_at: Optional[datetime] = None
    lldp_error: Optional[str] = None
    neighbour_count: int


class LldpComparisonResponse(BaseModel):
    filters: CablingFilters
    generated_at: datetime
    switches: List[LldpSwitchStatus]
    results: List[LldpResult]
    summary: Dict[str, int]


@router.get("/lldp", response_model=LldpComparisonResponse)
def get_lldp_comparison(
    auth: AdminDep,
    db: DbDep,
    location_id: Optional[int] = Query(None, ge=1),
    rack_id: Optional[int] = Query(None, ge=1),
):
    """Recorded cables compared with the stored LLDP neighbours of the switches in scope."""
    return compare_lldp(db, location_id=location_id, rack_id=rack_id)
