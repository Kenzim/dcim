"""
Admin API for client permission presets.

Presets are named, reusable maps of permission key -> bool (see
``app.core.client_permissions`` for the catalog). They can be assigned to a
``Product`` (catalog default), a ``User`` (per-client default), or a
``Service`` (per-service override) — see ``app.services.client_permission_resolver``
for how these layers combine.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.client_permissions import ALL_PERMISSION_KEYS, PERMISSION_CATALOG
from app.dao.permission_set_dao import PermissionSetDAO

router = APIRouter(prefix="/admin/permission-sets", tags=["admin-permission-sets"])


def _validate_permission_keys(permissions: Dict[str, Any]) -> None:
    unknown = sorted(set(permissions.keys()) - ALL_PERMISSION_KEYS)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permission key(s): {unknown}",
        )


class PermissionSetCreate(BaseModel):
    name: str
    description: Optional[str] = None
    permissions: Dict[str, bool] = Field(default_factory=dict)


class PermissionSetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[Dict[str, bool]] = None


class PermissionSetResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    permissions: Dict[str, bool]
    is_system: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


def _to_response(row) -> PermissionSetResponse:
    return PermissionSetResponse(
        id=row.id,
        name=row.name,
        description=row.description,
        permissions=row.permissions or {},
        is_system=row.is_system,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("/catalog", response_model=List[Dict[str, Any]])
async def get_permission_catalog(
    auth: dict = Depends(require_admin),
):
    """The fixed catalog of permission keys, grouped by applicable service type."""
    return PERMISSION_CATALOG


@router.get("", response_model=List[PermissionSetResponse])
async def list_permission_sets(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return [_to_response(row) for row in PermissionSetDAO.get_all(db)]


@router.post("", response_model=PermissionSetResponse, status_code=status.HTTP_201_CREATED)
async def create_permission_set(
    data: PermissionSetCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _validate_permission_keys(data.permissions)
    if PermissionSetDAO.get_by_name(db, data.name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A permission set with this name already exists")
    row = PermissionSetDAO.create(db, name=data.name, description=data.description, permissions=data.permissions)
    return _to_response(row)


@router.get("/{permission_set_id}", response_model=PermissionSetResponse)
async def get_permission_set(
    permission_set_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = PermissionSetDAO.get_by_id(db, permission_set_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission set not found")
    return _to_response(row)


@router.put("/{permission_set_id}", response_model=PermissionSetResponse)
async def update_permission_set(
    permission_set_id: int,
    data: PermissionSetUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = PermissionSetDAO.get_by_id(db, permission_set_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission set not found")
    update_data = data.model_dump(exclude_unset=True)
    if "permissions" in update_data and update_data["permissions"] is not None:
        _validate_permission_keys(update_data["permissions"])
    if "name" in update_data and update_data["name"] and update_data["name"] != row.name:
        existing = PermissionSetDAO.get_by_name(db, update_data["name"])
        if existing and existing.id != row.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A permission set with this name already exists")
    PermissionSetDAO.update(db, row, **update_data)
    return _to_response(row)


@router.delete("/{permission_set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_permission_set(
    permission_set_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = PermissionSetDAO.get_by_id(db, permission_set_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission set not found")
    if row.is_system:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="System permission sets cannot be deleted")
    PermissionSetDAO.delete(db, permission_set_id)
    return None
