"""
Admin API endpoints for managing services and external users.

These endpoints are for admin users to view and manage services
and external users created via billing integrations.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao.service_dao import ServiceDAO
from app.dao.external_user_dao import ExternalUserDAO
from app.dao.server_dao import ServerDAO
from app.models.service import ServiceStatus
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/services", tags=["admin-services"])


class ExternalUserResponse(BaseModel):
    id: int
    integration_id: int
    integration_name: str
    external_user_id: str
    external_username: Optional[str] = None
    external_email: Optional[str] = None
    created_at: str
    updated_at: str
    service_count: int = 0
    
    class Config:
        from_attributes = True


class ServiceResponse(BaseModel):
    id: int
    name: str
    external_service_id: Optional[str] = None
    server_id: int
    server_name: str
    external_user_id: int
    external_user_external_id: str
    status: str
    description: Optional[str] = None
    config: Optional[dict] = None
    created_at: str
    updated_at: str
    terminated_at: Optional[str] = None
    
    class Config:
        from_attributes = True


@router.get("/external-users", response_model=List[ExternalUserResponse])
async def list_external_users(
    integration_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all external users"""
    from app.models.external_user import ExternalUser
    
    query = db.query(ExternalUser)
    if integration_id:
        query = query.filter(ExternalUser.integration_id == integration_id)
    
    # Apply pagination
    external_users = query.order_by(ExternalUser.created_at.desc()).offset(skip).limit(limit).all()
    
    result = []
    for eu in external_users:
        # Get service count
        services = ServiceDAO.get_by_external_user(db, eu.id)
        
        result.append(ExternalUserResponse(
            id=eu.id,
            integration_id=eu.integration_id,
            integration_name=eu.integration.name,
            external_user_id=eu.external_user_id,
            external_username=eu.external_username,
            external_email=eu.external_email,
            created_at=eu.created_at.isoformat(),
            updated_at=eu.updated_at.isoformat(),
            service_count=len(services)
        ))
    
    return result


@router.get("/external-users/{external_user_id}", response_model=ExternalUserResponse)
async def get_external_user(
    external_user_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get external user details"""
    external_user = ExternalUserDAO.get_by_id(db, external_user_id)
    if not external_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="External user not found"
        )
    
    services = ServiceDAO.get_by_external_user(db, external_user.id)
    
    return ExternalUserResponse(
        id=external_user.id,
        integration_id=external_user.integration_id,
        integration_name=external_user.integration.name,
        external_user_id=external_user.external_user_id,
        external_username=external_user.external_username,
        external_email=external_user.external_email,
        created_at=external_user.created_at.isoformat(),
        updated_at=external_user.updated_at.isoformat(),
        service_count=len(services)
    )


@router.get("", response_model=List[ServiceResponse])
async def list_services(
    status_filter: Optional[str] = None,
    external_user_id: Optional[int] = None,
    server_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all services"""
    from app.models.service import Service
    
    query = db.query(Service)
    
    # Apply filters
    if status_filter:
        try:
            status_enum = ServiceStatus(status_filter.lower())
            query = query.filter(Service.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status: {status_filter}"
            )
    
    if external_user_id:
        query = query.filter(Service.external_user_id == external_user_id)
    
    if server_id:
        query = query.filter(Service.server_id == server_id)
    
    services = query.order_by(Service.name).offset(skip).limit(limit).all()
    
    result = []
    for service in services:
        server = service.server
        external_user = service.external_user
        
        result.append(ServiceResponse(
            id=service.id,
            name=service.name,
            external_service_id=service.external_service_id,
            server_id=service.server_id,
            server_name=server.name,
            external_user_id=service.external_user_id,
            external_user_external_id=external_user.external_user_id,
            status=service.status.value,
            description=service.description,
            config=service.config,
            created_at=service.created_at.isoformat(),
            updated_at=service.updated_at.isoformat(),
            terminated_at=service.terminated_at.isoformat() if service.terminated_at else None
        ))
    
    return result


@router.get("/{service_id}", response_model=ServiceResponse)
async def get_service(
    service_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get service details"""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    server = service.server
    external_user = service.external_user
    
    return ServiceResponse(
        id=service.id,
        name=service.name,
        external_service_id=service.external_service_id,
        server_id=service.server_id,
        server_name=server.name,
        external_user_id=service.external_user_id,
        external_user_external_id=external_user.external_user_id,
        status=service.status.value,
        description=service.description,
        config=service.config,
        created_at=service.created_at.isoformat(),
        updated_at=service.updated_at.isoformat(),
        terminated_at=service.terminated_at.isoformat() if service.terminated_at else None
    )
