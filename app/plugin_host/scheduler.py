"""Plugin schedules with database leases (docs/plugin-system.md section 9.3)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.plugin_host import PluginInstallation, PluginSchedule, PluginStatus
from app.models.service import Service
from app.plugin_host import health, runtime, service_links
from app.plugin_host.common import aware, new_session, utcnow
from app.plugin_host.sdk import service_info

logger = logging.getLogger(__name__)

MAX_STARTS_PER_PLUGIN = 20
MAX_CONCURRENT = 4
LEASE_MARGIN_SECONDS = 60


def scope_of(service_id: Optional[int]) -> str:
    return "global" if service_id is None else f"service:{int(service_id)}"


def stable_offset(service_id: int, jitter_seconds: int) -> int:
    if jitter_seconds <= 0:
        return 0
    return (int(service_id) * 2654435761) % (jitter_seconds + 1)


def sync_rows(db: Session) -> int:
    """Create rows for new schedules and newly active services; drop rows that no longer apply."""
    created = 0
    now = utcnow()
    for inst in runtime.enabled_installations(db):
        try:
            manifest = runtime.manifest_of(inst)
        except Exception:  # noqa: BLE001
            continue
        existing = {
            (row.schedule, row.scope): row
            for row in db.execute(select(PluginSchedule).where(PluginSchedule.plugin_id == inst.id)).scalars()
        }
        wanted: set[tuple[str, str]] = set()
        for spec in manifest.schedules:
            if spec.scope == "global":
                targets: list[Optional[int]] = [None]
            else:
                targets = list(service_links.active_service_ids(db, inst.id, manifest))
            for service_id in targets:
                key = (spec.id, scope_of(service_id))
                wanted.add(key)
                if key in existing:
                    continue
                offset = stable_offset(service_id, spec.jitter_seconds) if service_id is not None else 0
                try:
                    with db.begin_nested():
                        db.add(
                            PluginSchedule(
                                plugin_id=inst.id,
                                schedule=spec.id,
                                scope=key[1],
                                service_id=service_id,
                                next_run_at=now + timedelta(seconds=offset),
                            )
                        )
                        db.flush()
                    created += 1
                except IntegrityError:
                    pass
        stale = [row.id for key, row in existing.items() if key not in wanted]
        if stale:
            db.execute(delete(PluginSchedule).where(PluginSchedule.id.in_(stale)).execution_options(synchronize_session=False))
    db.commit()
    return created


def try_lease(db: Session, row_id: int, owner: str, seconds: int) -> bool:
    now = utcnow()
    result = db.execute(
        update(PluginSchedule)
        .where(
            PluginSchedule.id == row_id,
            PluginSchedule.next_run_at <= now,
            or_(PluginSchedule.lease_owner.is_(None), PluginSchedule.lease_expires_at.is_(None),
                PluginSchedule.lease_expires_at <= now),
        )
        .values(lease_owner=owner, lease_expires_at=now + timedelta(seconds=seconds))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def claim_due(db: Session, owner: str) -> list[int]:
    now = utcnow()
    claimed: list[int] = []
    for inst in runtime.enabled_installations(db):
        if health.circuit_open(inst, now):
            continue
        try:
            manifest = runtime.manifest_of(inst)
        except Exception:  # noqa: BLE001
            continue
        rows = db.execute(
            select(PluginSchedule.id, PluginSchedule.schedule)
            .where(
                PluginSchedule.plugin_id == inst.id,
                PluginSchedule.next_run_at <= now,
                or_(PluginSchedule.lease_owner.is_(None), PluginSchedule.lease_expires_at <= now),
            )
            .order_by(PluginSchedule.next_run_at)
            .limit(MAX_STARTS_PER_PLUGIN)
        ).all()
        for row_id, schedule_id in rows:
            spec = manifest.schedule(schedule_id)
            if spec is None:
                continue
            if try_lease(db, row_id, owner, spec.timeout + LEASE_MARGIN_SECONDS):
                claimed.append(row_id)
    return claimed


async def run_row(row_id: int, owner: str) -> Optional[str]:
    from app.plugin_host.invoke import invoke

    db = new_session()
    try:
        row = db.get(PluginSchedule, row_id)
        if row is None or row.lease_owner != owner:
            return None
        inst = db.get(PluginInstallation, row.plugin_id)
        if inst is None or inst.status != PluginStatus.ENABLED:
            return None
        manifest = runtime.manifest_of(inst)
        spec = manifest.schedule(row.schedule)
        if spec is None:
            return None
        service_arg = None
        if row.service_id is not None:
            service = db.get(Service, row.service_id)
            if service is None or not service_links.is_active(db, inst.id, manifest, service):
                db.delete(row)
                db.commit()
                return "skipped"
            service_arg = service_info(service)
        previous_next = aware(row.next_run_at)
        service_id = row.service_id
        db.expunge(inst)
    finally:
        db.close()

    outcome = await invoke(inst, "schedule", spec.id, (service_arg,), timeout=float(spec.timeout), service_id=service_id)
    now = utcnow()
    every = timedelta(seconds=spec.every_seconds)
    next_run = (previous_next or now) + every
    # Not run (plugin unavailable, or no free slot): skipped, and tried again in a minute.
    not_run = outcome.unavailable or outcome.busy
    if not_run:
        next_run = now + timedelta(seconds=60)
    elif next_run <= now:
        next_run = now + every
    status = "ok" if outcome.ok else ("skipped" if not_run else "failed")
    db = new_session()
    try:
        db.execute(
            update(PluginSchedule)
            .where(PluginSchedule.id == row_id, PluginSchedule.lease_owner == owner)
            .values(
                last_run_at=now if not not_run else PluginSchedule.last_run_at,
                last_status=status,
                last_error=None if outcome.ok else (outcome.error or outcome.refused or "failed")[:2000],
                next_run_at=next_run,
                lease_owner=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
    finally:
        db.close()
    return status


async def run_once(owner: Optional[str] = None) -> dict[str, int]:
    owner = owner or f"plugin-scheduler:{uuid.uuid4()}"
    db = new_session()
    try:
        sync_rows(db)
        ids = claim_due(db, owner)
    finally:
        db.close()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    counts: dict[str, int] = {}

    async def one(row_id: int) -> None:
        async with semaphore:
            try:
                status = await run_row(row_id, owner)
            except Exception:  # noqa: BLE001
                logger.exception("Plugin schedule row %s failed", row_id)
                status = "error"
        key = status or "lost"
        counts[key] = counts.get(key, 0) + 1

    await asyncio.gather(*(one(i) for i in ids))
    return counts
