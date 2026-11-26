"""OS install credentials stored on a server, and who may be shown them.

``Server.credentials`` holds the keys of the install that is currently on the
machine. Each install replaces the column, a termination of the server's
current live service clears it, and a service payload only carries the keys of
the template named by ``template_id``.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from sqlalchemy.orm import Session

from app.dao.service_dao import ServiceDAO
from app.models.service import Service, ServiceStatus, ServiceType
from app.services.os_template_service import get_template_service
from app.services.service_resource import is_current_occupant, service_linked_server

CREDENTIAL_META_KEYS = ("os_type", "template_id", "last_updated")


def build_install_credentials(
    template: Any,
    template_id: str,
    created_at: Any,
    template_parameters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """The whole credentials column for one install (replaces whatever was stored)."""
    credentials: Dict[str, Any] = {
        "os_type": template.os_type if template else None,
        "template_id": template_id,
        "last_updated": created_at.isoformat() if created_at else None,
    }
    for name, value in (template_parameters or {}).items():
        credentials[name] = value
    return credentials


def _template_parameter_names(template_id: Optional[str]) -> Iterable[str]:
    if not template_id:
        return ()
    template = get_template_service().get_template(template_id)
    if template is None:
        return ()
    return tuple((getattr(template, "parameters", None) or {}).keys())


def current_install_credentials(stored: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Only the keys of the install named by ``template_id``; stale keys never pass."""
    if not stored:
        return None
    allowed = set(CREDENTIAL_META_KEYS) | set(_template_parameter_names(stored.get("template_id")))
    return {key: value for key, value in stored.items() if key in allowed}


def _as_utc(value: Any) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _installed_after_every_earlier_service(db: Session, server: Any, installed_at: datetime) -> bool:
    for other in ServiceDAO.get_by_server(db, server.id):
        if other.status != ServiceStatus.TERMINATED:
            return False
        ended_at = _as_utc(other.terminated_at)
        if ended_at is None or installed_at <= ended_at:
            return False
    return True


def clear_credentials_for_new_allocation(db: Session, server: Any) -> None:
    """Drop the keys a server holds when it is handed to a new service, unless they are a pre-install.

    Keys stay only when their ``last_updated`` (the start of the install that wrote
    them) is later than the end of every earlier service on the server and no
    other service on it is live, i.e. staff installed the spare server before the
    customer's service existed. An unknown install time clears.

    Runs inside the allocation transaction, before the new install writes its
    own keys; the caller commits and already holds the server or group lock.
    """
    stored = server.credentials
    if not stored:
        return
    installed_at = _as_utc(stored.get("last_updated"))
    if installed_at is None or not _installed_after_every_earlier_service(db, server, installed_at):
        server.credentials = None


def clear_credentials_on_terminate(db: Session, service: Service) -> bool:
    """Clear the server's credentials when the service being terminated is its current live service.

    Call before the service is marked terminated. A service that is already
    terminated, superseded or sharing the server with another live service
    leaves the column alone. The caller commits.
    """
    if service.service_type != ServiceType.BARE_METAL:
        return False
    server = service_linked_server(db, service, include_history=True)
    if not is_current_occupant(db, service, server):
        return False
    server.credentials = None
    return True
