from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import List, Literal
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import CableRunDAO, SwitchPortDAO, NetworkPortDAO
from app.dao.patch_panel_dao import PatchPanelDAO
from app.models.cable_run import CableRun
from app.services.cable_trace_service import describe_path, describe_ref, trace_from_port
import logging

from typing import Annotated
DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

logger = logging.getLogger(__name__)

router = APIRouter()


class PortRef(BaseModel):
    type: str  # "server" | "switch" | "patch"
    id: int  # port id (network_ports.id, switch_ports.id or patch_panel_ports.id)
    side: Literal["front", "rear"] | None = None  # required for patch panel ports


class CableRunCreate(BaseModel):
    port_a: PortRef
    port_b: PortRef
    cable_type: str | None = None
    speed_mbps: int | None = None
    description: str | None = None


class CableRunUpdate(BaseModel):
    cable_type: str | None = None
    speed_mbps: int | None = None
    description: str | None = None


class CableRunEnd(BaseModel):
    type: str  # "server" | "switch" | "patch"
    id: int  # port id
    side: str | None = None  # "front" | "rear" for patch panel ports
    port_name: str | None = None
    device_id: int | None = None
    device_name: str | None = None


class CableRunResponse(BaseModel):
    id: int
    end_a: CableRunEnd
    end_b: CableRunEnd
    cable_type: str | None
    speed_mbps: int | None
    description: str | None

    class Config:
        from_attributes = True


def _end_from_cable_run(db: Session, cable_run: CableRun, is_end_a: bool) -> CableRunEnd:
    """Build CableRunEnd for end_a (is_end_a=True) or end_b (is_end_a=False)."""
    ref = cable_run.end("a" if is_end_a else "b")
    info = describe_ref(db, ref) or {"type": ref[0], "id": ref[1] or 0, "side": ref[2]}
    return CableRunEnd(**info)


def _cable_run_to_response(db: Session, cable_run: CableRun) -> CableRunResponse:
    return CableRunResponse(
        id=cable_run.id,
        end_a=_end_from_cable_run(db, cable_run, True),
        end_b=_end_from_cable_run(db, cable_run, False),
        cable_type=cable_run.cable_type,
        speed_mbps=cable_run.speed_mbps,
        description=cable_run.description,
    )


@router.post("/", response_model=CableRunResponse, status_code=status.HTTP_201_CREATED)
async def create_cable_run(
    cable_data: CableRunCreate,
    auth: AdminDep,
    db: DbDep
):
    """Create a cable run between two ports (each can be a switch port or server port)."""
    lookups = {"switch": SwitchPortDAO.get_by_id, "server": NetworkPortDAO.get_by_id, "patch": PatchPanelDAO.get_port}
    for ref in (cable_data.port_a, cable_data.port_b):
        if ref.type not in lookups:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"port type must be 'server', 'switch' or 'patch', got '{ref.type}'"
            )
        if ref.type == "patch" and ref.side is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="patch panel ports need side 'front' or 'rear'"
            )
        if not lookups[ref.type](db, ref.id):
            label = "Patch panel" if ref.type == "patch" else ref.type.capitalize()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{label} port {ref.id} not found"
            )
    try:
        cable_run = CableRunDAO.create(
            db,
            port_a_type=cable_data.port_a.type,
            port_a_id=cable_data.port_a.id,
            port_b_type=cable_data.port_b.type,
            port_b_id=cable_data.port_b.id,
            cable_type=cable_data.cable_type,
            speed_mbps=cable_data.speed_mbps,
            description=cable_data.description,
            port_a_side=cable_data.port_a.side,
            port_b_side=cable_data.port_b.side,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    return _cable_run_to_response(db, cable_run)


@router.get("/", response_model=List[CableRunResponse])
async def list_cable_runs(
    skip: int = 0,
    limit: int = 100,
    switch_id: int | None = None,
    server_id: int | None = None,
    *,
    auth: AdminDep,
    db: DbDep
):
    """List cable runs, optionally filtered by switch_id or server_id."""
    # Coerce to int so query params passed as strings (e.g. ?server_id=1) always match DB integers
    sid = int(server_id) if server_id is not None else None
    swid = int(switch_id) if switch_id is not None else None
    if swid is not None:
        cable_runs = CableRunDAO.get_by_switch(db, swid)
    elif sid is not None:
        cable_runs = CableRunDAO.get_by_server(db, sid)
    else:
        cable_runs = CableRunDAO.get_all(db, skip=skip, limit=limit)
    return [_cable_run_to_response(db, cr) for cr in cable_runs]


@router.get("/trace")
async def trace_cable_path(
    auth: AdminDep,
    db: DbDep,
    port_type: Literal["server", "switch", "patch"] = Query(..., alias="type"),
    port_id: int = Query(..., alias="id"),
    side: Literal["front", "rear"] | None = None,
):
    """Follow the cable on a port through any patch panels to the device at the far end.
    For a patch panel port, side is the side to start from (the path leaves through that side)."""
    if port_type == "patch" and side is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="side is required for patch panel ports")
    return describe_path(db, trace_from_port(db, port_type, port_id, side if port_type == "patch" else None))


@router.get("/{cable_run_id}", response_model=CableRunResponse)
async def get_cable_run(
    cable_run_id: int,
    auth: AdminDep,
    db: DbDep
):
    """Get a cable run by ID."""
    cable_run = CableRunDAO.get_by_id(db, cable_run_id)
    if not cable_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cable run not found"
        )
    return _cable_run_to_response(db, cable_run)


@router.put("/{cable_run_id}", response_model=CableRunResponse)
async def update_cable_run(
    cable_run_id: int,
    cable_data: CableRunUpdate,
    auth: AdminDep,
    db: DbDep
):
    """Update a cable run (metadata only; endpoints cannot be changed)."""
    cable_run = CableRunDAO.get_by_id(db, cable_run_id)
    if not cable_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cable run not found"
        )
    if cable_data.cable_type is not None:
        cable_run.cable_type = cable_data.cable_type
    if cable_data.speed_mbps is not None:
        cable_run.speed_mbps = cable_data.speed_mbps
    if cable_data.description is not None:
        cable_run.description = cable_data.description
    cable_run = CableRunDAO.update(db, cable_run)
    return _cable_run_to_response(db, cable_run)


@router.delete("/{cable_run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cable_run(
    cable_run_id: int,
    auth: AdminDep,
    db: DbDep
):
    """Delete a cable run."""
    success = CableRunDAO.delete(db, cable_run_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cable run not found"
        )
