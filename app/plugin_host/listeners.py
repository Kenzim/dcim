"""Service lifecycle events from every code path, via session flush events (docs/plugin-system.md section 6.2).

``before_flush`` notes new services and status changes; ``after_flush`` (when ids are
known) writes the outbox rows on the flush's own connection, so they commit or roll back
with the change that caused them.
"""

from __future__ import annotations

import enum
import logging
from typing import Any, Optional

from sqlalchemy import event, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import attributes as orm_attributes

from app.models.service import Service, ServiceStatus
from app.plugin_host import runtime

logger = logging.getLogger(__name__)

_INFO_KEY = "rackflow_plugin_events"
SERVICE_EVENTS = (
    "service.created",
    "service.activated",
    "service.suspended",
    "service.unsuspended",
    "service.terminated",
)


def _value(v: Any) -> Any:
    return v.value if isinstance(v, enum.Enum) else v


def _transition(old: Optional[ServiceStatus], new: ServiceStatus) -> Optional[str]:
    if old == new:
        return None
    if new == ServiceStatus.SUSPENDED:
        return "service.suspended"
    if new == ServiceStatus.TERMINATED:
        return "service.terminated"
    if new == ServiceStatus.ACTIVE and old == ServiceStatus.SUSPENDED:
        return "service.unsuspended"
    if new == ServiceStatus.ACTIVE and old == ServiceStatus.PENDING:
        return "service.activated"
    return None


def _wanted(session: Session) -> set[str]:
    with session.no_autoflush:
        events = runtime.subscriptions(session).events
    return {name for name in SERVICE_EVENTS if events.get(name)}


def _before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    try:
        new_services = [o for o in session.new if isinstance(o, Service)]
        dirty_services = [o for o in session.dirty if isinstance(o, Service)]
        if not new_services and not dirty_services:
            return
        wanted = _wanted(session)
        if not wanted:
            return
        pending = session.info.setdefault(_INFO_KEY, [])
        if "service.created" in wanted:
            pending.extend(("service.created", svc, None) for svc in new_services)
        for svc in dirty_services:
            history = orm_attributes.get_history(svc, "status")
            if not history.added:
                continue
            new = history.added[0]
            if history.deleted:
                old = history.deleted[0]
            else:
                with session.no_autoflush:
                    old = session.execute(select(Service.status).where(Service.id == svc.id)).scalar()
            name = _transition(old, new)
            if name in wanted:
                pending.append((name, svc, old))
    except Exception:  # noqa: BLE001 - never break the flush
        logger.warning("Collecting plugin service events failed", exc_info=True)
        session.info.pop(_INFO_KEY, None)


def _payload(name: str, svc: Service, old: Any) -> dict:
    base = {
        "service_id": svc.id,
        "service_type": _value(svc.service_type),
        "owner_user_id": svc.owner_user_id,
    }
    if name == "service.created":
        base.update(
            status=_value(svc.status),
            product_code=svc.product_code,
            os_code=svc.os_code,
            provisioning_source=_value(svc.provisioning_source),
        )
    elif name == "service.unsuspended":
        base["status"] = _value(svc.status)
    else:
        base["previous_status"] = _value(old)
    return base


def _after_flush(session: Session, flush_context: Any) -> None:
    pending = session.info.pop(_INFO_KEY, None)
    if not pending:
        return
    from app.plugin_host.events import emit_on_connection

    try:
        connection = session.connection()
        for name, svc, old in pending:
            if svc.id is None:
                continue
            emit_on_connection(connection, session, name, _payload(name, svc, old), svc.id)
    except Exception:  # noqa: BLE001
        logger.warning("Queueing plugin service events failed", exc_info=True)


def _after_rollback(session: Session) -> None:
    session.info.pop(_INFO_KEY, None)


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_flush", _after_flush)
    event.listen(Session, "after_soft_rollback", lambda session, previous: _after_rollback(session))
    _installed = True
