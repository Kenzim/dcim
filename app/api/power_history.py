"""Power history API (admin): graph data for PDUs, inlets, power feeds, racks, servers and locations."""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao import LocationDAO, RackDAO, ServerDAO
from app.dao.pdu_dao import PduDAO, PowerFeedDAO
from app.services import power_history_service as history

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]
HoursQuery = Annotated[
    int,
    Query(ge=1, le=24 * 400, description="Range in hours, 24 up to about a year; clamped to the hourly retention window"),
]

router = APIRouter()


def _found(item, what: str):
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")
    return item


@router.get("/pdus/{pdu_id}/history", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_pdu_history(pdu_id: int, auth: AdminDep, db: DbDep, hours: HoursQuery = 24):
    """Power, current, apparent power and voltage of the PDU, each inlet and each bank (with its rated current), plus energy used."""
    return history.pdu_history(db, _found(PduDAO.get_by_id(db, pdu_id), "PDU"), hours)


@router.get("/pdus/{pdu_id}/inlets/{inlet_id}/history", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_inlet_history(pdu_id: int, inlet_id: int, auth: AdminDep, db: DbDep, hours: HoursQuery = 24):
    """History of one PDU inlet (an ATS input reads zero while it is not the active one)."""
    inlet = _found(PduDAO.get_inlet(db, inlet_id), "PDU inlet")
    if inlet.pdu_id != pdu_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDU inlet not found")
    return history.inlet_history(db, inlet, hours)


@router.get("/power-feeds/{feed_id}/history", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_feed_history(feed_id: int, auth: AdminDep, db: DbDep, hours: HoursQuery = 24):
    """Power and current drawn on a feed (sum of its inlets) with the feed's capacity limits."""
    return history.feed_history(db, _found(PowerFeedDAO.get_by_id(db, feed_id), "Power feed"), hours)


@router.get("/racks/{rack_id}/power-history", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_rack_power_history(rack_id: int, auth: AdminDep, db: DbDep, hours: HoursQuery = 24):
    """Power drawn by the PDUs of a rack."""
    rack = _found(RackDAO.get_by_id(db, rack_id), "Rack")
    return history.rack_history(db, rack.id, rack.name, hours)


@router.get("/servers/{server_id}/power-history", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_server_power_history(server_id: int, auth: AdminDep, db: DbDep, hours: HoursQuery = 24):
    """Power of a server from its metered outlets; `available` and `reason` say when it cannot be known."""
    return history.server_history(db, _found(ServerDAO.get_by_id(db, server_id), "Server"), hours)


@router.get("/power-history", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_power_history(
    auth: AdminDep, db: DbDep, hours: HoursQuery = 24, location_id: Optional[int] = None
):
    """Total power of all PDUs, or of one location (location_id)."""
    name = None
    if location_id is not None:
        name = _found(LocationDAO.get_by_id(db, location_id), "Location").name
    return history.location_history(db, location_id, name, hours)
