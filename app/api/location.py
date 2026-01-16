from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import LocationDAO
from app.models.location import Location

router = APIRouter()


class LocationCreate(BaseModel):
    name: str
    description: str | None = None


class LocationResponse(BaseModel):
    id: int
    name: str
    description: str | None

    class Config:
        from_attributes = True


@router.get("/", response_model=List[LocationResponse])
async def list_locations(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all locations"""
    locations = LocationDAO.get_all(db)
    return locations


@router.post("/", response_model=LocationResponse, status_code=status.HTTP_201_CREATED)
async def create_location(
    location_data: LocationCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Create a new location"""
    # Check if location with same name already exists
    existing = LocationDAO.get_by_name(db, location_data.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Location with this name already exists"
        )
    
    location = LocationDAO.create(
        db,
        name=location_data.name,
        description=location_data.description
    )
    return location


@router.get("/{location_id}", response_model=LocationResponse)
async def get_location(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get a location by ID"""
    location = LocationDAO.get_by_id(db, location_id)
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found"
        )
    return location


@router.put("/{location_id}", response_model=LocationResponse)
async def update_location(
    location_id: int,
    location_data: LocationCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Update a location"""
    location = LocationDAO.get_by_id(db, location_id)
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found"
        )
    
    # Check if name is being changed and if new name already exists
    if location_data.name != location.name:
        existing = LocationDAO.get_by_name(db, location_data.name)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Location with this name already exists"
            )
    
    location.name = location_data.name
    location.description = location_data.description
    return LocationDAO.update(db, location)


@router.delete("/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_location(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete a location"""
    success = LocationDAO.delete(db, location_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found"
        )



