from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import RackDAO, LocationDAO
from app.models.rack import Rack

router = APIRouter()


class RackCreate(BaseModel):
    location_id: int
    name: str
    units: int = 42
    units_start_from_bottom: bool = True
    description: str | None = None
    row: int | None = None
    row_position: int | None = None


class RackUpdate(BaseModel):
    name: str | None = None
    units: int | None = None
    units_start_from_bottom: bool | None = None
    description: str | None = None
    row: int | None = None
    row_position: int | None = None


class RackResponse(BaseModel):
    id: int
    location_id: int
    name: str
    description: str | None
    units: int
    units_start_from_bottom: bool = True
    row: int | None = None
    row_position: int | None = None

    class Config:
        from_attributes = True


@router.get("/", response_model=List[RackResponse])
async def list_racks(
    location_id: int | None = None,
    row: int | None = None,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all racks, optionally filtered by location and/or row"""
    if location_id and row is not None:
        racks = RackDAO.get_by_row(db, location_id, row)
    elif location_id:
        racks = RackDAO.get_by_location(db, location_id)
    else:
        racks = RackDAO.get_all(db)
    return racks


@router.post("/", response_model=RackResponse, status_code=status.HTTP_201_CREATED)
async def create_rack(
    rack_data: RackCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Create a new rack"""
    # Validate location exists
    location = LocationDAO.get_by_id(db, rack_data.location_id)
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found"
        )
    
    # Validate units
    if rack_data.units < 1 or rack_data.units > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Units must be between 1 and 100"
        )
    
    # Check if rack with same name already exists in this location
    existing = RackDAO.get_by_name_and_location(db, rack_data.name, rack_data.location_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rack with this name already exists in this location"
        )
    
    rack = RackDAO.create(
        db,
        location_id=rack_data.location_id,
        name=rack_data.name,
        units=rack_data.units,
        units_start_from_bottom=rack_data.units_start_from_bottom,
        description=rack_data.description,
        row=rack_data.row,
        row_position=rack_data.row_position
    )
    return rack


@router.get("/{rack_id}", response_model=RackResponse)
async def get_rack(
    rack_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get a rack by ID"""
    rack = RackDAO.get_by_id(db, rack_id)
    if not rack:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rack not found"
        )
    return rack


@router.put("/{rack_id}", response_model=RackResponse)
async def update_rack(
    rack_id: int,
    rack_data: RackUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Update a rack"""
    rack = RackDAO.get_by_id(db, rack_id)
    if not rack:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rack not found"
        )
    
    # Validate units if provided
    if rack_data.units is not None:
        if rack_data.units < 1 or rack_data.units > 100:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Units must be between 1 and 100"
            )
    
    # Update unit numbering orientation if provided
    if rack_data.units_start_from_bottom is not None:
        rack.units_start_from_bottom = rack_data.units_start_from_bottom
    
    # Check if name is being changed and if new name already exists in this location
    if rack_data.name is not None and rack_data.name != rack.name:
        existing = RackDAO.get_by_name_and_location(db, rack_data.name, rack.location_id)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Rack with this name already exists in this location"
            )
        rack.name = rack_data.name
    
    if rack_data.units is not None:
        rack.units = rack_data.units
    if rack_data.description is not None:
        rack.description = rack_data.description
    if rack_data.row is not None:
        rack.row = rack_data.row
    if rack_data.row_position is not None:
        rack.row_position = rack_data.row_position
    
    return RackDAO.update(db, rack)


@router.delete("/{rack_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rack(
    rack_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete a rack"""
    success = RackDAO.delete(db, rack_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rack not found"
        )


@router.get("/{rack_id}/servers", response_model=List[dict])
async def get_rack_servers(
    rack_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get all servers in a rack, organized by rack unit"""
    rack = RackDAO.get_by_id(db, rack_id)
    if not rack:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rack not found"
        )
    
    # Get all servers in this rack
    servers = []
    for server in rack.servers:
        servers.append({
            "id": server.id,
            "name": server.name,
            "rack_unit": server.rack_unit,
            "rack_units": getattr(server, "rack_units", 1) or 1,
            "server_ip": server.server_ip,
            "description": server.description
        })
    
    return servers
