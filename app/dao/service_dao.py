from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.service import Service, ServiceStatus


class ServiceDAO:
    """Data Access Object for Service model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        server_id: int,
        external_user_id: int,
        external_service_id: Optional[str] = None,
        status: ServiceStatus = ServiceStatus.PENDING,
        description: Optional[str] = None,
        config: Optional[dict] = None
    ) -> Service:
        """Create a new service"""
        service = Service(
            name=name,
            external_service_id=external_service_id,
            server_id=server_id,
            external_user_id=external_user_id,
            status=status,
            description=description,
            config=config or {}
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service

    @staticmethod
    def get_by_id(db: Session, service_id: int) -> Optional[Service]:
        """Get service by ID"""
        return db.query(Service).filter(Service.id == service_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[Service]:
        """Get service by name"""
        return db.query(Service).filter(Service.name == name).first()

    @staticmethod
    def get_by_external_service_id(db: Session, external_service_id: str) -> Optional[Service]:
        """Get service by external service ID"""
        return db.query(Service).filter(Service.external_service_id == external_service_id).first()

    @staticmethod
    def get_by_external_user(db: Session, external_user_id: int) -> List[Service]:
        """Get all services for an external user"""
        return db.query(Service).filter(Service.external_user_id == external_user_id).all()

    @staticmethod
    def get_by_server(db: Session, server_id: int) -> List[Service]:
        """Get all services for a server"""
        return db.query(Service).filter(Service.server_id == server_id).all()

    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100, status: Optional[ServiceStatus] = None) -> List[Service]:
        """Get all services with pagination"""
        query = db.query(Service)
        if status:
            query = query.filter(Service.status == status)
        return query.order_by(Service.name).offset(skip).limit(limit).all()

    @staticmethod
    def update(db: Session, service: Service) -> Service:
        """Update a service"""
        db.commit()
        db.refresh(service)
        return service

    @staticmethod
    def delete(db: Session, service_id: int) -> bool:
        """Delete a service by ID"""
        service = db.query(Service).filter(Service.id == service_id).first()
        if service:
            db.delete(service)
            db.commit()
            return True
        return False
