from sqlalchemy.orm import Session
from typing import Optional, Dict, Any, List
from app.models.server_activity import (
    ServerActivity,
    ServerActivityEventType,
    ServerActivityStatus,
)


class ServerActivityDAO:
    """Data access for server activity log entries."""

    @staticmethod
    def create(
        db: Session,
        *,
        event_type: ServerActivityEventType,
        action: str,
        status: ServerActivityStatus,
        message: str,
        source: str,
        server_id: Optional[int] = None,
        service_id: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> ServerActivity:
        if (server_id is None) == (service_id is None):
            raise ValueError("Exactly one of server_id, service_id must be set")
        entry = ServerActivity(
            server_id=server_id,
            service_id=service_id,
            event_type=event_type,
            action=action,
            status=status,
            message=message,
            source=source,
            details=details or {},
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    @staticmethod
    def get_by_server(db: Session, server_id: int, limit: int = 100) -> List[ServerActivity]:
        return (
            db.query(ServerActivity)
            .filter(ServerActivity.server_id == server_id)
            .order_by(ServerActivity.created_at.desc(), ServerActivity.id.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_by_service(db: Session, service_id: int, limit: int = 100) -> List[ServerActivity]:
        return (
            db.query(ServerActivity)
            .filter(ServerActivity.service_id == service_id)
            .order_by(ServerActivity.created_at.desc(), ServerActivity.id.desc())
            .limit(limit)
            .all()
        )
