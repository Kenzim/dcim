"""The event bus: emit events into the outbox and run before hooks (docs/plugin-system.md section 6)."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.models.plugin_host import DeliveryStatus, PluginDelivery, PluginInstallation, PluginStatus
from app.models.service import Service, ServiceType
from app.plugin_host import runtime, service_links
from app.plugin_host.catalogue import API_VERSION, EVENTS, POWER_ACTIONS, POWER_SOURCES
from app.plugin_host.common import audit, new_session, utcnow

logger = logging.getLogger(__name__)

HOOK_TIMEOUT = 2.0
HOOKS_TOTAL = 3.0


class OperationVetoed(Exception):
    """A plugin's before hook refused the operation. The message is safe to show."""

    def __init__(self, plugin_id: str, message: str) -> None:
        super().__init__(message)
        self.plugin_id = plugin_id
        self.message = message


def envelope(name: str, payload: dict[str, Any], service_id: Optional[int] = None) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "version": API_VERSION,
        "occurred_at": utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "service_id": service_id,
        "payload": dict(payload),
    }


def _check_payload(name: str, payload: dict) -> None:
    spec = EVENTS[name]
    missing = [k for k in spec.required if k not in payload]
    if missing:
        logger.warning("Event %s emitted without %s", name, ", ".join(missing))


def _queue_rows(connection, env: dict, plugin_ids: list[str]) -> int:
    if not plugin_ids:
        return 0
    now = utcnow()
    rows = [
        {
            "event_id": env["id"],
            "plugin_id": plugin_id,
            "event": env["name"],
            "service_id": env["service_id"],
            "envelope": env,
            "status": DeliveryStatus.QUEUED,
            "attempts": 0,
            "next_attempt_at": now,
            "created_at": now,
        }
        for plugin_id in plugin_ids
    ]
    connection.execute(insert(PluginDelivery.__table__), rows)
    return len(rows)


def _plugins_for(db: Session, name: str, service_type: Optional[str]) -> list[str]:
    subs = runtime.subscriptions(db)
    plugin_ids = list(subs.events.get(name, []))
    if not plugin_ids or service_type is None:
        return plugin_ids
    rows = db.execute(
        select(PluginInstallation.id, PluginInstallation.manifest).where(PluginInstallation.id.in_(plugin_ids))
    ).all()
    wanted = []
    for plugin_id, manifest in rows:
        types = ((manifest or {}).get("services") or {}).get("types") or []
        if service_type in types:
            wanted.append(plugin_id)
    return sorted(wanted)


def emit(db: Session, name: str, payload: dict[str, Any], *, service_id: Optional[int] = None) -> int:
    """Queue an event for subscribed plugins in the caller's transaction. Never raises.

    Events in the commerce webhook catalogue also go to outbound webhooks. Returns the
    number of plugin deliveries queued.
    """
    if name not in EVENTS:
        logger.error("Unknown plugin event %s", name)
        return 0
    try:
        from app.services.commerce_webhook_service import COMMERCE_WEBHOOK_EVENTS, CommerceWebhookService

        if name in COMMERCE_WEBHOOK_EVENTS:
            CommerceWebhookService.enqueue(db, name, payload)
    except Exception:  # noqa: BLE001 - webhook queueing keeps its old best-effort behaviour
        logger.exception("Queueing %s webhooks failed", name)
    try:
        _check_payload(name, payload)
        service_type = payload.get("service_type") if EVENTS[name].service_scoped else None
        with db.no_autoflush:
            plugin_ids = _plugins_for(db, name, service_type)
            if not plugin_ids:
                return 0
            return _queue_rows(db.connection(), envelope(name, payload, service_id), plugin_ids)
    except Exception:  # noqa: BLE001 - emitting must never break the operation that caused it
        logger.exception("Emitting plugin event %s failed", name)
        return 0


def emit_on_connection(connection, db: Session, name: str, payload: dict[str, Any], service_id: Optional[int]) -> int:
    """Emit from inside a flush, writing straight to the flush's connection."""
    try:
        _check_payload(name, payload)
        service_type = payload.get("service_type") if EVENTS[name].service_scoped else None
        plugin_ids = _plugins_for(db, name, service_type)
        return _queue_rows(connection, envelope(name, payload, service_id), plugin_ids)
    except Exception:  # noqa: BLE001
        logger.exception("Emitting plugin event %s failed", name)
        return 0


def emit_detached(name: str, payload: dict[str, Any], *, service_id: Optional[int] = None) -> int:
    """Emit and commit on a session of its own, for paths that do not commit."""
    db = new_session()
    try:
        count = emit(db, name, payload, service_id=service_id)
        db.commit()
        return count
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Emitting plugin event %s failed", name)
        return 0
    finally:
        db.close()


async def run_before(
    db: Session,
    name: str,
    payload: dict[str, Any],
    *,
    service: Optional[Service] = None,
    exclude_plugin: Optional[str] = None,
) -> None:
    """Run before hooks in plugin id order. Raises OperationVetoed on the first veto.

    Hooks that fail or time out count against the plugin's health and allow the operation.
    """
    from app.plugin_host.invoke import invoke
    from app.plugin_host.sdk import Alter, Veto

    try:
        plugin_ids = [p for p in runtime.subscriptions(db).hooks.get(name, []) if p != exclude_plugin]
    except Exception:  # noqa: BLE001
        logger.exception("Loading plugin hooks for %s failed", name)
        return
    if not plugin_ids:
        return
    env = envelope(name, payload, service.id if service is not None else None)
    deadline = time.monotonic() + HOOKS_TOTAL
    for plugin_id in plugin_ids:
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            logger.warning("Before hooks for %s ran out of time; the rest were skipped", name)
            return
        inst = db.get(PluginInstallation, plugin_id)
        if inst is None or inst.status != PluginStatus.ENABLED:
            continue
        if service is not None:
            try:
                manifest = runtime.manifest_of(inst)
            except Exception:  # noqa: BLE001
                continue
            if not service_links.is_active(db, plugin_id, manifest, service):
                continue
        outcome = await invoke(inst, "before", name, (env,), timeout=min(HOOK_TIMEOUT, remaining),
                               service_id=env["service_id"])
        if not outcome.ok:
            continue
        if isinstance(outcome.value, Veto):
            message = f"{inst.name}: {outcome.value.reason}"
            audit(
                "plugin.veto",
                plugin_id=plugin_id,
                resource_type="service" if service is not None else "plugin",
                resource_id=str(service.id) if service is not None else plugin_id,
                metadata={"event": name, "reason": outcome.value.reason},
            )
            raise OperationVetoed(plugin_id, message)
        if isinstance(outcome.value, Alter) and outcome.value.fields:
            mutable = set(EVENTS[name].mutable)
            for key, value in outcome.value.fields.items():
                if key in mutable:
                    payload[key] = value


def _type_value(service: Service) -> str:
    kind = service.service_type
    return kind.value if isinstance(kind, ServiceType) else str(kind)


async def guard_power(
    db: Session,
    service: Service,
    action: str,
    *,
    source: str,
    actor_user_id: Optional[int] = None,
) -> None:
    """power_requested for HTTP routes: a veto becomes HTTP 409. Unknown actions are left to the caller."""
    action = (action or "").strip().lower()
    if action not in POWER_ACTIONS and action not in ("start", "stop", "power_on", "power_off", "restart"):
        return
    try:
        await power_requested(db, service, action, source=source, actor_user_id=actor_user_id)
    except OperationVetoed as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail=exc.message) from exc


async def power_requested(
    db: Session,
    service: Service,
    action: str,
    *,
    source: str,
    actor_user_id: Optional[int] = None,
    exclude_plugin: Optional[str] = None,
) -> None:
    """Run power before hooks (raises OperationVetoed) and queue service.power_requested."""
    if action not in POWER_ACTIONS:
        action = {"start": "on", "stop": "off", "power_on": "on", "power_off": "off", "restart": "reboot"}.get(
            action, action
        )
    if action not in POWER_ACTIONS:
        return
    payload = {
        "service_id": service.id,
        "service_type": _type_value(service),
        "action": action,
        "source": source if source in POWER_SOURCES else "admin",
        "actor_user_id": actor_user_id,
    }
    await run_before(db, "service.power_requested", payload, service=service, exclude_plugin=exclude_plugin)
    try:
        wanted = bool(runtime.subscriptions(db).events.get("service.power_requested"))
    except Exception:  # noqa: BLE001
        logger.exception("Loading plugin subscriptions failed")
        wanted = False
    if wanted:
        emit_detached("service.power_requested", payload, service_id=service.id)
