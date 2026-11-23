"""Deliver queued events to plugin handlers (docs/plugin-system.md section 6.4)."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Optional

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from app.models.plugin_host import DeliveryStatus, PluginDelivery, PluginInstallation, PluginStatus
from app.models.service import Service
from app.plugin_host import health, runtime, service_links
from app.plugin_host.common import new_session, utcnow

logger = logging.getLogger(__name__)

DELIVERY_TIMEOUT = 30.0
MAX_ATTEMPTS = 10
DISABLED_EXPIRY = timedelta(hours=24)
CLAIM_SECONDS = 120
KEEP_DONE = timedelta(days=7)
KEEP_DEAD = timedelta(days=30)


def backoff_seconds(attempts: int) -> int:
    return min(3600, 30 * 2 ** max(0, attempts - 1))


def _locked(statement, db: Session):
    if db.get_bind().dialect.name in {"mysql", "postgresql"}:
        return statement.with_for_update(skip_locked=True)
    return statement.with_for_update()


def claim(db: Session, owner: str, batch_size: int = 50) -> list[int]:
    now = utcnow()
    enabled = select(PluginInstallation.id).where(
        PluginInstallation.status == PluginStatus.ENABLED,
        or_(PluginInstallation.circuit_open_until.is_(None), PluginInstallation.circuit_open_until <= now),
    )
    due = or_(PluginDelivery.next_attempt_at.is_(None), PluginDelivery.next_attempt_at <= now)
    available = or_(
        and_(PluginDelivery.status.in_([DeliveryStatus.QUEUED, DeliveryStatus.FAILED]), due),
        and_(PluginDelivery.status == DeliveryStatus.SENDING, PluginDelivery.claim_expires_at <= now),
    )
    statement = (
        select(PluginDelivery)
        .where(available, PluginDelivery.plugin_id.in_(enabled))
        .order_by(PluginDelivery.id)
        .limit(batch_size)
    )
    rows = list(db.execute(_locked(statement, db)).scalars())
    for row in rows:
        row.status = DeliveryStatus.SENDING
        row.claim_token = owner
        row.claim_expires_at = now + timedelta(seconds=CLAIM_SECONDS)
    db.commit()
    return [row.id for row in rows]


def _finish(row_id: int, owner: str, **values) -> None:
    db = new_session()
    try:
        db.execute(
            update(PluginDelivery)
            .where(PluginDelivery.id == row_id, PluginDelivery.claim_token == owner)
            .values(claim_token=None, claim_expires_at=None, **values)
            .execution_options(synchronize_session=False)
        )
        db.commit()
    finally:
        db.close()


async def deliver_one(row_id: int, owner: str) -> str:
    from app.plugin_host.invoke import invoke

    db = new_session()
    try:
        row = db.get(PluginDelivery, row_id)
        if row is None or row.claim_token != owner:
            return "lost"
        inst = db.get(PluginInstallation, row.plugin_id)
        envelope = dict(row.envelope or {})
        event = row.event
        attempts = row.attempts or 0
        service_id = row.service_id
        if inst is None:
            _finish(row_id, owner, status=DeliveryStatus.DEAD, last_error="The plugin is no longer installed")
            return DeliveryStatus.DEAD
        if inst.status != PluginStatus.ENABLED or health.circuit_open(inst):
            _finish(row_id, owner, status=DeliveryStatus.QUEUED, next_attempt_at=utcnow() + timedelta(seconds=60))
            return DeliveryStatus.QUEUED
        if service_id is not None:
            service = db.get(Service, service_id)
            try:
                manifest = runtime.manifest_of(inst)
                active = service is not None and (
                    service_links.is_active(db, inst.id, manifest, service)
                    or event == "service.terminated"
                    and service_links.applies_to_type(manifest, service)
                    and _was_active(db, inst.id, manifest, service)
                )
            except Exception:  # noqa: BLE001
                active = False
            if not active:
                _finish(row_id, owner, status=DeliveryStatus.SKIPPED, last_error="The plugin is not active for the service")
                return DeliveryStatus.SKIPPED
        db.expunge(inst)
    finally:
        db.close()

    outcome = await invoke(inst, "event", event, (envelope,), timeout=DELIVERY_TIMEOUT, service_id=service_id)
    if outcome.ok:
        _finish(row_id, owner, status=DeliveryStatus.DELIVERED, delivered_at=utcnow(), attempts=attempts + 1,
                last_error=None)
        return DeliveryStatus.DELIVERED
    if outcome.unavailable or outcome.busy:
        # Not attempted: the plugin was unavailable or had no free slot; try again later.
        _finish(row_id, owner, status=DeliveryStatus.QUEUED, next_attempt_at=utcnow() + timedelta(seconds=60))
        return DeliveryStatus.QUEUED
    attempts += 1
    error = (outcome.error or outcome.refused or "failed")[:2000]
    if attempts >= MAX_ATTEMPTS:
        _finish(row_id, owner, status=DeliveryStatus.DEAD, attempts=attempts, last_error=error)
        return DeliveryStatus.DEAD
    _finish(row_id, owner, status=DeliveryStatus.FAILED, attempts=attempts, last_error=error,
            next_attempt_at=utcnow() + timedelta(seconds=backoff_seconds(attempts)))
    return DeliveryStatus.FAILED


def _was_active(db: Session, plugin_id: str, manifest, service: Service) -> bool:
    """A terminated service is never active; deliver its termination when it was active before."""
    link = service_links.link_of(db, plugin_id, service.id)
    if link is not None:
        return bool(link.enabled)
    return manifest.services is not None and manifest.services.enablement == "all"


async def run_once(owner: Optional[str] = None, batch_size: int = 50) -> dict[str, int]:
    owner = owner or f"plugin-outbox:{uuid.uuid4()}"
    db = new_session()
    try:
        ids = claim(db, owner, batch_size)
    finally:
        db.close()
    counts: dict[str, int] = {}
    for row_id in ids:
        try:
            status = await deliver_one(row_id, owner)
        except Exception:  # noqa: BLE001 - one bad row never stops the batch
            logger.exception("Plugin delivery %s failed", row_id)
            status = "error"
        counts[status] = counts.get(status, 0) + 1
    return counts


def expire_disabled(db: Session) -> int:
    """Queued deliveries of plugins that stayed disabled for a day become dead letters.

    A disabled installation's ``updated_at`` is when it stopped running (nothing else
    updates it while disabled), so both it and the delivery must be older than a day.
    """
    cutoff = utcnow() - DISABLED_EXPIRY
    disabled = select(PluginInstallation.id).where(
        PluginInstallation.status != PluginStatus.ENABLED,
        PluginInstallation.updated_at < cutoff,
    )
    result = db.execute(
        update(PluginDelivery)
        .where(
            PluginDelivery.status.in_([DeliveryStatus.QUEUED, DeliveryStatus.FAILED]),
            PluginDelivery.plugin_id.in_(disabled),
            PluginDelivery.created_at < cutoff,
        )
        .values(status=DeliveryStatus.DEAD, last_error="The plugin was disabled for more than 24 hours")
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def prune(db: Session) -> int:
    now = utcnow()
    done = db.execute(
        delete(PluginDelivery)
        .where(
            PluginDelivery.status.in_([DeliveryStatus.DELIVERED, DeliveryStatus.SKIPPED]),
            PluginDelivery.created_at < now - KEEP_DONE,
        )
        .execution_options(synchronize_session=False)
    ).rowcount or 0
    dead = db.execute(
        delete(PluginDelivery)
        .where(PluginDelivery.status == DeliveryStatus.DEAD, PluginDelivery.created_at < now - KEEP_DEAD)
        .execution_options(synchronize_session=False)
    ).rowcount or 0
    return done + dead


def retry(db: Session, plugin_id: str, delivery_id: int) -> bool:
    row = db.get(PluginDelivery, delivery_id)
    if row is None or row.plugin_id != plugin_id or row.status not in (DeliveryStatus.DEAD, DeliveryStatus.FAILED):
        return False
    row.status = DeliveryStatus.QUEUED
    row.attempts = 0
    row.next_attempt_at = utcnow()
    row.claim_token = None
    row.claim_expires_at = None
    return True


def counts(db: Session, plugin_id: str) -> dict[str, int]:
    from sqlalchemy import func

    rows = db.execute(
        select(PluginDelivery.status, func.count(PluginDelivery.id))
        .where(PluginDelivery.plugin_id == plugin_id)
        .group_by(PluginDelivery.status)
    ).all()
    found = {status: n for status, n in rows}
    queued = sum(found.get(s, 0) for s in (DeliveryStatus.QUEUED, DeliveryStatus.FAILED, DeliveryStatus.SENDING))
    return {"queued": queued, "dead": found.get(DeliveryStatus.DEAD, 0)}
