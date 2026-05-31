from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
import logging

from app.dao.server_activity_dao import ServerActivityDAO
from app.models.server_activity import ServerActivityEventType, ServerActivityStatus

logger = logging.getLogger(__name__)

_SENSITIVE_DETAIL_KEYS = {
    "password",
    "token",
    "secret",
    "api_key",
    "script_content",
    "credentials",
    "authorization",
}


def _sanitize_details(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for key, val in value.items():
            if key.lower() in _SENSITIVE_DETAIL_KEYS:
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_details(val)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_details(v) for v in value]
    if isinstance(value, str) and len(value) > 1000:
        return value[:1000] + "...[truncated]"
    return value


def _resolve_target(
    *,
    server_id: Optional[int],
    service_id: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    if (server_id is None) == (service_id is None):
        raise ValueError("Exactly one of server_id, service_id must be set")
    return server_id, service_id


def log_server_activity(
    db: Session,
    *,
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    event_type: ServerActivityEventType,
    action: str,
    status: ServerActivityStatus,
    message: str,
    source: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist a server/service activity entry without breaking caller flow on logger failures."""
    try:
        sid, scid = _resolve_target(server_id=server_id, service_id=service_id)
        ServerActivityDAO.create(
            db=db,
            server_id=sid,
            service_id=scid,
            event_type=event_type,
            action=action,
            status=status,
            message=message,
            source=source,
            details=_sanitize_details(details or {}),
        )
    except Exception as exc:
        logger.warning("Failed to persist server activity log: %s", exc, exc_info=True)


def log_server_activity_attempt(
    db: Session,
    *,
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    event_type: ServerActivityEventType,
    action: str,
    source: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    log_server_activity(
        db,
        server_id=server_id,
        service_id=service_id,
        event_type=event_type,
        action=action,
        status=ServerActivityStatus.ATTEMPT,
        message=message,
        source=source,
        details=details,
    )


def log_server_activity_success(
    db: Session,
    *,
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    event_type: ServerActivityEventType,
    action: str,
    source: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    log_server_activity(
        db,
        server_id=server_id,
        service_id=service_id,
        event_type=event_type,
        action=action,
        status=ServerActivityStatus.SUCCESS,
        message=message,
        source=source,
        details=details,
    )


def log_server_activity_failure(
    db: Session,
    *,
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    event_type: ServerActivityEventType,
    action: str,
    source: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    error: Optional[Exception] = None,
) -> None:
    payload = details or {}
    if error is not None:
        payload = {
            **payload,
            "error": str(error)[:500],
        }

    log_server_activity(
        db,
        server_id=server_id,
        service_id=service_id,
        event_type=event_type,
        action=action,
        status=ServerActivityStatus.FAILED,
        message=message,
        source=source,
        details=payload,
    )
