"""Server power inputs (PSUs): linking to PDU outlets, power usage and outlet control from the server."""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.pdu import OutletAction, _admin_source, run_outlet_action
from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao import ServerDAO
from app.dao.pdu_dao import PduDAO, ServerPowerInputDAO
from app.models.server import Server
from app.models.server_power_input import ServerPowerInput
from app.services import power_service

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter()


class PowerInputCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: Optional[str] = None
    pdu_outlet_id: Optional[int] = None


class PowerInputUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = None
    pdu_outlet_id: Optional[int] = None  # Send null to unplug


def _get_server_or_404(db: Session, server_id: int) -> Server:
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


def _get_input_or_404(db: Session, server: Server, input_id: int) -> ServerPowerInput:
    spi = ServerPowerInputDAO.get_by_id(db, input_id)
    if not spi or spi.server_id != server.id:
        raise HTTPException(status_code=404, detail="Power input not found")
    return spi


def _link_outlet(db: Session, spi: ServerPowerInput, outlet_id: Optional[int]) -> None:
    if outlet_id is None:
        spi.pdu_outlet_id = None
        return
    outlet = PduDAO.get_outlet(db, outlet_id)
    if not outlet:
        raise HTTPException(status_code=404, detail="PDU outlet not found")
    holder = ServerPowerInputDAO.get_by_outlet(db, outlet_id)
    if holder and holder.id != spi.id:
        who = f"{holder.server.name} {holder.name}" if holder.server else holder.name
        raise HTTPException(status_code=409, detail=f"Outlet {outlet.position} on {outlet.pdu.name} already feeds {who}")
    spi.pdu_outlet_id = outlet_id


@router.get("/{server_id}/power-inputs", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_server_power(server_id: int, auth: AdminDep, db: DbDep):
    """Power inputs, the outlets and feeds behind them, and total draw where outlets are metered."""
    return power_service.server_power_summary(db, _get_server_or_404(db, server_id))


@router.post(
    "/{server_id}/power-inputs", response_model=dict, status_code=status.HTTP_201_CREATED,
    responses=COMMON_ERROR_RESPONSES,
)
async def add_power_input(server_id: int, data: PowerInputCreate, auth: AdminDep, db: DbDep):
    server = _get_server_or_404(db, server_id)
    spi = ServerPowerInput(server_id=server.id, name=data.name, description=data.description)
    db.add(spi)
    db.flush()
    _link_outlet(db, spi, data.pdu_outlet_id)
    db.commit()
    return power_service.server_power_summary(db, server)


@router.put("/{server_id}/power-inputs/{input_id}", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def update_power_input(server_id: int, input_id: int, data: PowerInputUpdate, auth: AdminDep, db: DbDep):
    server = _get_server_or_404(db, server_id)
    spi = _get_input_or_404(db, server, input_id)
    if data.name is not None:
        spi.name = data.name
    if "description" in data.model_fields_set:
        spi.description = data.description
    if "pdu_outlet_id" in data.model_fields_set:
        _link_outlet(db, spi, data.pdu_outlet_id)
    db.commit()
    return power_service.server_power_summary(db, server)


@router.delete(
    "/{server_id}/power-inputs/{input_id}", status_code=status.HTTP_204_NO_CONTENT,
    responses=COMMON_ERROR_RESPONSES,
)
async def delete_power_input(server_id: int, input_id: int, auth: AdminDep, db: DbDep):
    server = _get_server_or_404(db, server_id)
    spi = _get_input_or_404(db, server, input_id)
    db.delete(spi)
    db.commit()


@router.post(
    "/{server_id}/power-inputs/{input_id}/outlet-action", response_model=dict,
    responses=COMMON_ERROR_RESPONSES,
)
async def power_input_outlet_action(
    server_id: int, input_id: int, data: OutletAction, auth: AdminDep, db: DbDep
):
    """Switch the PDU outlet feeding this power input (needs confirm=true and a switched, enabled PDU)."""
    server = _get_server_or_404(db, server_id)
    spi = _get_input_or_404(db, server, input_id)
    if spi.pdu_outlet is None:
        raise HTTPException(status_code=400, detail="Power input is not plugged into a PDU outlet")
    return await run_outlet_action(db, spi.pdu_outlet, data, source=_admin_source(auth))
