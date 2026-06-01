"""Data access for service instances (per-location DHCP/TFTP runners).

API keys are stored in plaintext in the database so that deployments
do not depend on an additional encryption key.
"""
from typing import List, Optional
from sqlalchemy.orm import Session
from app.models.service_instance import ServiceInstance


class ServiceInstanceDAO:
    @staticmethod
    def create(
        db: Session,
        location_id: int,
        service_type: str,
        name: str,
        base_url: str,
        api_key: Optional[str] = None,
    ) -> Optional[ServiceInstance]:
        row = ServiceInstance(
            location_id=location_id,
            service_type=service_type,
            name=name,
            base_url=base_url.rstrip("/"),
            api_key_encrypted=(api_key or ""),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_by_id(db: Session, instance_id: int) -> Optional[ServiceInstance]:
        return db.query(ServiceInstance).filter(ServiceInstance.id == instance_id).first()

    @staticmethod
    def get_all(db: Session, location_id: Optional[int] = None) -> List[ServiceInstance]:
        q = db.query(ServiceInstance)
        if location_id is not None:
            q = q.filter(ServiceInstance.location_id == location_id)
        return q.order_by(ServiceInstance.location_id, ServiceInstance.service_type).all()

    @staticmethod
    def get_by_location_and_type(db: Session, location_id: int, service_type: str) -> Optional[ServiceInstance]:
        return (
            db.query(ServiceInstance)
            .filter(
                ServiceInstance.location_id == location_id,
                ServiceInstance.service_type == service_type,
            )
            .first()
        )

    @staticmethod
    def get_api_key(row: ServiceInstance) -> Optional[str]:
        """Return the stored API key (plaintext)."""
        return row.api_key_encrypted or None

    @staticmethod
    def update(
        db: Session,
        row: ServiceInstance,
        name: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> ServiceInstance:
        if name is not None:
            row.name = name
        if base_url is not None:
            row.base_url = base_url.rstrip("/")
        if api_key is not None:
            row.api_key_encrypted = api_key
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def update_connection_test(db: Session, row: ServiceInstance, ok: bool) -> ServiceInstance:
        from datetime import datetime, timezone
        row.last_connection_test = datetime.now(timezone.utc)
        row.connection_ok = ok
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, instance_id: int) -> bool:
        row = ServiceInstanceDAO.get_by_id(db, instance_id)
        if row:
            db.delete(row)
            db.commit()
            return True
        return False

    @staticmethod
    def verify_api_key(row: ServiceInstance, api_key: str) -> bool:
        """Verify provided api_key matches stored (simple string compare).

        When no key is stored, verification always fails; callers can choose
        to skip verification in that case.
        """
        stored = row.api_key_encrypted or ""
        return stored != "" and stored == (api_key or "")
