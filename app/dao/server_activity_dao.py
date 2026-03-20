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
        server_id: int,
        event_type: ServerActivityEventType,
        action: str,
        status: ServerActivityStatus,
        message: str,
        source: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> ServerActivity:
        entry = ServerActivity(
            server_id=server_id,
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
