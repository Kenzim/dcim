"""Power feed API: circuits supplied to racks, entered by hand."""
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao import RackDAO
from app.dao.pdu_dao import PowerFeedDAO
from app.models.power_feed import PowerFeed
from app.services import power_service

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter()

PHASES = ("single", "three")


class PowerFeedCreate(BaseModel):
    rack_id: int
    name: str = Field(min_length=1, max_length=64)
    voltage: int = Field(default=208, ge=1, le=1000)
    amperage: int = Field(default=30, ge=1, le=1000)
    phase: str = "single"
    max_utilisation_percent: int = Field(default=80, ge=1, le=100)
    description: Optional[str] = None


class PowerFeedUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    voltage: Optional[int] = Field(default=None, ge=1, le=1000)
    amperage: Optional[int] = Field(default=None, ge=1, le=1000)
    phase: Optional[str] = None
    max_utilisation_percent: Optional[int] = Field(default=None, ge=1, le=100)
    description: Optional[str] = None


def _check_phase(phase: Optional[str]) -> None:
    if phase is not None and phase not in PHASES:
        raise HTTPException(status_code=400, detail="phase must be 'single' or 'three'")


def _get_feed_or_404(db: Session, feed_id: int) -> PowerFeed:
    feed = PowerFeedDAO.get_by_id(db, feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="Power feed not found")
    return feed


@router.get("/", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_power_feeds(rack_id: Optional[int] = None, *, auth: AdminDep, db: DbDep):
    """Power feeds with live usage, optionally for one rack."""
    feeds = PowerFeedDAO.get_by_rack(db, rack_id) if rack_id is not None else PowerFeedDAO.get_all(db)
    return [power_service.feed_summary(f) for f in feeds]


@router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
async def create_power_feed(data: PowerFeedCreate, auth: AdminDep, db: DbDep):
    if not RackDAO.get_by_id(db, data.rack_id):
        raise HTTPException(status_code=404, detail="Rack not found")
    _check_phase(data.phase)
    if PowerFeedDAO.get_by_rack_and_name(db, data.rack_id, data.name):
        raise HTTPException(status_code=400, detail="A power feed with this name already exists in the rack")
    feed = PowerFeed(**data.model_dump())
    db.add(feed)
    db.commit()
    db.refresh(feed)
    return power_service.feed_summary(feed)


@router.get("/{feed_id}", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_power_feed(feed_id: int, auth: AdminDep, db: DbDep):
    return power_service.feed_summary(_get_feed_or_404(db, feed_id))


@router.put("/{feed_id}", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def update_power_feed(feed_id: int, data: PowerFeedUpdate, auth: AdminDep, db: DbDep):
    feed = _get_feed_or_404(db, feed_id)
    _check_phase(data.phase)
    if data.name is not None and data.name != feed.name:
        if PowerFeedDAO.get_by_rack_and_name(db, feed.rack_id, data.name):
            raise HTTPException(status_code=400, detail="A power feed with this name already exists in the rack")
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is not None or field == "description":
            setattr(feed, field, value)
    db.commit()
    db.refresh(feed)
    return power_service.feed_summary(feed)


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
async def delete_power_feed(feed_id: int, auth: AdminDep, db: DbDep):
    """Delete a feed; PDU inlets linked to it are unlinked."""
    feed = _get_feed_or_404(db, feed_id)
    for inlet in feed.pdu_inlets:
        inlet.power_feed_id = None
    db.delete(feed)
    db.commit()
