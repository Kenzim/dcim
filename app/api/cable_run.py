from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import CableRunDAO, SwitchPortDAO, NetworkPortDAO
from app.models.cable_run import CableRun
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


class PortRef(BaseModel):
    type: str  # "server" | "switch"
    id: int  # port id (network_ports.id or switch_ports.id)


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
    type: str  # "server" | "switch"
    id: int  # port id
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
    if is_end_a:
        port_id = cable_run.end_a_switch_port_id or cable_run.end_a_server_port_id
        port_type = "switch" if cable_run.end_a_switch_port_id else "server"
    else:
        port_id = cable_run.end_b_switch_port_id or cable_run.end_b_server_port_id
        port_type = "switch" if cable_run.end_b_switch_port_id else "server"
    port_name = None
    device_id = None
    device_name = None
    if port_type == "switch":
        port = SwitchPortDAO.get_by_id(db, port_id) if port_id else None
        if port:
            port_name = port.name
            device_id = port.switch_id
            device_name = port.switch.name if port.switch else None
    else:
        port = NetworkPortDAO.get_by_id(db, port_id) if port_id else None
        if port:
            port_name = port.name
            device_id = port.server_id
            device_name = port.server.name if port.server else None
    return CableRunEnd(type=port_type, id=port_id or 0, port_name=port_name, device_id=device_id, device_name=device_name)


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
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Create a cable run between two ports (each can be a switch port or server port)."""
    for ref in (cable_data.port_a, cable_data.port_b):
        if ref.type not in ("server", "switch"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"port type must be 'server' or 'switch', got '{ref.type}'"
            )
    # Validate port_a exists
    if cable_data.port_a.type == "switch":
        port_a = SwitchPortDAO.get_by_id(db, cable_data.port_a.id)
    else:
        port_a = NetworkPortDAO.get_by_id(db, cable_data.port_a.id)
    if not port_a:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{cable_data.port_a.type.capitalize()} port {cable_data.port_a.id} not found"
        )
    # Validate port_b exists
    if cable_data.port_b.type == "switch":
        port_b = SwitchPortDAO.get_by_id(db, cable_data.port_b.id)
    else:
        port_b = NetworkPortDAO.get_by_id(db, cable_data.port_b.id)
    if not port_b:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{cable_data.port_b.type.capitalize()} port {cable_data.port_b.id} not found"
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
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
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


@router.get("/{cable_run_id}", response_model=CableRunResponse)
async def get_cable_run(
    cable_run_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
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
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
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
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete a cable run."""
    success = CableRunDAO.delete(db, cable_run_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cable run not found"
        )
