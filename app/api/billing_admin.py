"""
Admin API endpoints for managing billing integrations.

These endpoints are for admin users to create, configure, and manage
billing integration instances.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.integrations.registry import get_integration_registry
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing/integrations", tags=["billing-admin"])


class BillingIntegrationCreate(BaseModel):
    name: str
    integration_type: str
    config: Optional[dict] = None
    description: Optional[str] = None
    enabled: bool = True


class BillingIntegrationUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


class BillingIntegrationResponse(BaseModel):
    id: int
    name: str
    integration_type: str
    api_key: str
    enabled: bool
    config: Optional[dict] = None
    description: Optional[str] = None
    created_at: str
    updated_at: str
    last_used_at: Optional[str] = None
    last_used_ip: Optional[str] = None
    
    class Config:
        from_attributes = True


@router.post("", response_model=BillingIntegrationResponse, status_code=status.HTTP_201_CREATED)
async def create_integration(
    integration_data: BillingIntegrationCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Create a new billing integration"""
    # Validate integration type exists
    registry = get_integration_registry()
    if not registry.is_registered(integration_data.integration_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown integration type: {integration_data.integration_type}"
        )
    
    # Validate config if integration has custom validation
    integration_class = registry.get(integration_data.integration_type)
    if integration_class:
        integration_instance = integration_class()
        is_valid, error_msg = integration_instance.validate_config(integration_data.config or {})
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid configuration: {error_msg}"
            )
    
    # Create integration
    integration = BillingIntegrationDAO.create(
        db,
        name=integration_data.name,
        integration_type=integration_data.integration_type,
        config=integration_data.config,
        description=integration_data.description,
        enabled=integration_data.enabled
    )
    
    logger.info(f"Created billing integration '{integration.name}' (ID: {integration.id}, Type: {integration.integration_type})")
    
    return BillingIntegrationResponse(
        id=integration.id,
        name=integration.name,
        integration_type=integration.integration_type,
        api_key=integration.api_key,
        enabled=integration.enabled,
        config=integration.config,
        description=integration.description,
        created_at=integration.created_at.isoformat(),
        updated_at=integration.updated_at.isoformat(),
        last_used_at=integration.last_used_at.isoformat() if integration.last_used_at else None,
        last_used_ip=integration.last_used_ip
    )


@router.get("", response_model=List[BillingIntegrationResponse])
async def list_integrations(
    enabled_only: bool = False,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all billing integrations"""
    integrations = BillingIntegrationDAO.get_all(db, enabled_only=enabled_only)
    return [
        BillingIntegrationResponse(
            id=i.id,
            name=i.name,
            integration_type=i.integration_type,
            api_key=i.api_key,
            enabled=i.enabled,
            config=i.config,
            description=i.description,
            created_at=i.created_at.isoformat(),
            updated_at=i.updated_at.isoformat(),
            last_used_at=i.last_used_at.isoformat() if i.last_used_at else None,
            last_used_ip=i.last_used_ip
        )
        for i in integrations
    ]


@router.get("/types", response_model=List[dict])
async def list_integration_types(
    auth: dict = Depends(require_admin)
):
    """List available integration types"""
    registry = get_integration_registry()
    types = []
    for integration_type, integration_class in registry.get_all().items():
        instance = registry.create_instance(integration_type)
        if instance:
            types.append({
                "type": integration_type,
                "name": instance.get_name(),
                "description": instance.get_description(),
                "config_schema": instance.get_config_schema()
            })
    return types


@router.get("/{integration_id}", response_model=BillingIntegrationResponse)
async def get_integration(
    integration_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get integration details"""
    integration = BillingIntegrationDAO.get_by_id(db, integration_id)
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration not found"
        )
    
    return BillingIntegrationResponse(
        id=integration.id,
        name=integration.name,
        integration_type=integration.integration_type,
        api_key=integration.api_key,
        enabled=integration.enabled,
        config=integration.config,
        description=integration.description,
        created_at=integration.created_at.isoformat(),
        updated_at=integration.updated_at.isoformat(),
        last_used_at=integration.last_used_at.isoformat() if integration.last_used_at else None,
        last_used_ip=integration.last_used_ip
    )


@router.put("/{integration_id}", response_model=BillingIntegrationResponse)
async def update_integration(
    integration_id: int,
    integration_data: BillingIntegrationUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Update an integration"""
    integration = BillingIntegrationDAO.get_by_id(db, integration_id)
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration not found"
        )
    
    # Update fields
    if integration_data.name is not None:
        integration.name = integration_data.name
    if integration_data.config is not None:
        # Validate config if integration has custom validation
        registry = get_integration_registry()
        integration_class = registry.get(integration.integration_type)
        if integration_class:
            integration_instance = integration_class()
            is_valid, error_msg = integration_instance.validate_config(integration_data.config)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid configuration: {error_msg}"
                )
        integration.config = integration_data.config
    if integration_data.description is not None:
        integration.description = integration_data.description
    if integration_data.enabled is not None:
        integration.enabled = integration_data.enabled
    
    BillingIntegrationDAO.update(db, integration)
    db.refresh(integration)
    
    return BillingIntegrationResponse(
        id=integration.id,
        name=integration.name,
        integration_type=integration.integration_type,
        api_key=integration.api_key,
        enabled=integration.enabled,
        config=integration.config,
        description=integration.description,
        created_at=integration.created_at.isoformat(),
        updated_at=integration.updated_at.isoformat(),
        last_used_at=integration.last_used_at.isoformat() if integration.last_used_at else None,
        last_used_ip=integration.last_used_ip
    )


@router.post("/{integration_id}/rotate-key", response_model=BillingIntegrationResponse)
async def rotate_api_key(
    integration_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Generate a new API key for an integration"""
    integration = BillingIntegrationDAO.get_by_id(db, integration_id)
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration not found"
        )
    
    integration = BillingIntegrationDAO.rotate_api_key(db, integration)
    logger.info(f"Rotated API key for integration '{integration.name}' (ID: {integration.id})")
    
    return BillingIntegrationResponse(
        id=integration.id,
        name=integration.name,
        integration_type=integration.integration_type,
        api_key=integration.api_key,
        enabled=integration.enabled,
        config=integration.config,
        description=integration.description,
        created_at=integration.created_at.isoformat(),
        updated_at=integration.updated_at.isoformat(),
        last_used_at=integration.last_used_at.isoformat() if integration.last_used_at else None,
        last_used_ip=integration.last_used_ip
    )


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    integration_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete an integration"""
    success = BillingIntegrationDAO.delete(db, integration_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration not found"
        )
    
    logger.info(f"Deleted billing integration (ID: {integration_id})")
    return None
