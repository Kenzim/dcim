"""
Service instance API – per-location DHCP/TFTP runner registration.

CRUD for service instances. Test connection uses api_key to call the runner's /status.
Requires SERVICE_INSTANCE_ENCRYPTION_KEY for creating instances (API keys stored encrypted).
"""
from datetime import datetime
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import ServiceInstanceDAO, LocationDAO
from app.models.service_instance import ServiceInstance


router = APIRouter(prefix="/service-instances", tags=["service-instances"])


class ServiceInstanceCreate(BaseModel):
    location_id: int
    service_type: str  # 'dhcp' | 'tftp'
    name: str
    base_url: str
    api_key: str | None = None


class ServiceInstanceUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None


class ServiceInstanceResponse(BaseModel):
    id: int
    location_id: int
    service_type: str
    name: str
    base_url: str
    last_connection_test: Optional[datetime] = None
    connection_ok: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ServiceInstanceTestBody(BaseModel):
    api_key: Optional[str] = None


class ServiceInstanceTestResponse(BaseModel):
    success: bool
    message: str
    connection_ok: bool


def _call_runner_health(base_url: str, api_key: str, use_auth: bool) -> tuple[bool, str]:
    """Call runner /health (no auth) or /status (auth). Returns (ok, message)."""
    base_url = base_url.rstrip("/")
    # Call /status on the runner. When an API key is configured on the runner
    # we send it in X-API-Key; otherwise we call without auth.
    url = f"{base_url}/status"
    headers = {}
    if use_auth and api_key:
        headers["X-API-Key"] = api_key
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(url, headers=headers)
            if r.status_code == 200:
                return True, "Connection successful"
            if r.status_code == 401:
                return False, "Invalid API key"
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


@router.get("/", response_model=List[ServiceInstanceResponse])
async def list_service_instances(
    location_id: Optional[int] = None,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List service instances, optionally filtered by location_id."""
    instances = ServiceInstanceDAO.get_all(db, location_id=location_id)
    return instances


@router.post("/", response_model=ServiceInstanceResponse, status_code=status.HTTP_201_CREATED)
async def create_service_instance(
    data: ServiceInstanceCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new service instance."""
    if data.service_type not in ("dhcp", "tftp"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="service_type must be 'dhcp' or 'tftp'",
        )
    location = LocationDAO.get_by_id(db, data.location_id)
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found",
        )
    existing = ServiceInstanceDAO.get_by_location_and_type(db, data.location_id, data.service_type)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A {data.service_type} instance already exists for this location",
        )
    instance = ServiceInstanceDAO.create(
        db,
        location_id=data.location_id,
        service_type=data.service_type,
        name=data.name,
        base_url=data.base_url,
        api_key=data.api_key or "",
    )
    if not instance:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create service instance (encryption error)",
        )
    return instance


@router.get("/{instance_id}", response_model=ServiceInstanceResponse)
async def get_service_instance(
    instance_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get a service instance by ID."""
    instance = ServiceInstanceDAO.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service instance not found",
        )
    return instance


@router.put("/{instance_id}", response_model=ServiceInstanceResponse)
async def update_service_instance(
    instance_id: int,
    data: ServiceInstanceUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update a service instance."""
    instance = ServiceInstanceDAO.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service instance not found",
        )
    update_kw = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    instance = ServiceInstanceDAO.update(db, instance, **update_kw)
    return instance


@router.delete("/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_instance(
    instance_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a service instance."""
    instance = ServiceInstanceDAO.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service instance not found",
        )
    ServiceInstanceDAO.delete(db, instance_id)


@router.post("/{instance_id}/test", response_model=ServiceInstanceTestResponse)
async def test_service_instance(
    instance_id: int,
    body: ServiceInstanceTestBody,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Test connection to the runner. Requires api_key (we don't store plaintext)."""
    instance = ServiceInstanceDAO.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service instance not found",
        )
    # If an API key is stored for this instance, verify it matches the
    # provided key (when one is supplied). When no key is stored, skip
    # verification so unauthenticated runners can be used.
    if instance.api_key_encrypted:
        if not body.api_key or not ServiceInstanceDAO.verify_api_key(instance, body.api_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="API key does not match the stored key for this instance",
            )
    api_key = body.api_key or instance.api_key_encrypted or ""
    ok, msg = _call_runner_health(instance.base_url, api_key, use_auth=bool(api_key))
    ServiceInstanceDAO.update_connection_test(db, instance, ok)
    return ServiceInstanceTestResponse(success=ok, message=msg, connection_ok=ok)
