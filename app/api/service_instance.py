"""
Service instance API – per-location DHCP/TFTP runner registration.

CRUD for service instances. Test connection uses api_key to call the runner's /status.
Requires SERVICE_INSTANCE_ENCRYPTION_KEY for creating instances (API keys stored encrypted).
"""
from datetime import datetime
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, field_validator
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import ServiceInstanceDAO, LocationDAO
from app.models.service_instance import ServiceInstance


from typing import Annotated
DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/service-instances", tags=["service-instances"])

# DHCP/TFTP runner base_urls legitimately point at RFC1918 addresses
# inside the DC network by design (that's how these runners are deployed),
# so this intentionally does not allowlist/denylist IP ranges the way the
# Go proxy runner's customer-facing destination ACL does. It only rejects
# non-HTTP(S) schemes, which would let an admin-supplied value be used for
# SSRF-adjacent tricks (e.g. file://, gopher://) when passed to httpx.
# Proxy runners are managed separately via /api/admin/proxy-runners.
_ALLOWED_BASE_URL_SCHEMES = {"http", "https"}


def _validate_runner_base_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme.lower() not in _ALLOWED_BASE_URL_SCHEMES or not parsed.hostname:
        raise ValueError("base_url must be an http:// or https:// URL")
    return value


class ServiceInstanceCreate(BaseModel):
    location_id: int
    service_type: str  # 'dhcp' | 'tftp'
    name: str
    base_url: str
    api_key: str | None = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _validate_runner_base_url(value)


class ServiceInstanceUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: Optional[str]) -> Optional[str]:
        return _validate_runner_base_url(value)


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
    *,
    auth: AdminDep,
    db: DbDep,
):
    """List service instances, optionally filtered by location_id."""
    instances = ServiceInstanceDAO.get_all(db, location_id=location_id)
    return instances


@router.post("/", response_model=ServiceInstanceResponse, status_code=status.HTTP_201_CREATED)
async def create_service_instance(
    data: ServiceInstanceCreate,
    auth: AdminDep,
    db: DbDep,
):
    """Create a new service instance."""
    if data.service_type not in ("dhcp", "tftp"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="service_type must be 'dhcp' or 'tftp' (proxy runners use /api/admin/proxy-runners)",
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
    auth: AdminDep,
    db: DbDep,
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
    auth: AdminDep,
    db: DbDep,
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
    auth: AdminDep,
    db: DbDep,
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
    auth: AdminDep,
    db: DbDep,
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
        if not body.api_key or not ServiceInstanceDAO.verify_api_key(instance, body.api_key, db=db):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="API key does not match the stored key for this instance",
            )
    api_key = body.api_key or ServiceInstanceDAO.get_api_key(instance) or ""
    ok, msg = _call_runner_health(instance.base_url, api_key, use_auth=bool(api_key))
    ServiceInstanceDAO.update_connection_test(db, instance, ok)
    return ServiceInstanceTestResponse(success=ok, message=msg, connection_ok=ok)
