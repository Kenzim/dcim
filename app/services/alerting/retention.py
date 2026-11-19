"""Prune old alerts, silences, deliveries and backup run records in short batches."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.alerting import (
    Alert,
    AlertSilence,
    AlertState,
    DeliveryStatus,
    NotificationDelivery,
    VmBackupRun,
)
from app.services.alerting.catalogue import get_global_settings

logger = logging.getLogger(__name__)

BATCH = 1000
MAX_BATCHES = 50
SILENCE_KEEP_DAYS = 30
BACKUP_RUN_KEEP_DAYS = 90


def _purge(db: Session, model, condition) -> int:
    """Delete matching rows in batches of ``BATCH`` (ids first, so MySQL accepts it)."""
    total = 0
    for _ in range(MAX_BATCHES):
        ids = list(db.execute(select(model.id).where(condition).limit(BATCH)).scalars())
        if not ids:
            break
        db.execute(delete(model).where(model.id.in_(ids)).execution_options(synchronize_session=False))
        db.commit()
        total += len(ids)
        if len(ids) < BATCH:
            break
    return total


def prune(db: Session, now: datetime) -> dict[str, int]:
    cfg = get_global_settings(db)
    counts = {
        "alerts": _purge(
            db,
            Alert,
            (Alert.state == AlertState.RESOLVED)
            & (Alert.resolved_at < now - timedelta(days=cfg["alert_retention_days"])),
        ),
        "silences": _purge(db, AlertSilence, AlertSilence.ends_at < now - timedelta(days=SILENCE_KEEP_DAYS)),
        "deliveries_sent": _purge(
            db,
            NotificationDelivery,
            NotificationDelivery.status.in_([DeliveryStatus.SENT, DeliveryStatus.SKIPPED])
            & (NotificationDelivery.created_at < now - timedelta(days=cfg["delivery_retention_days"])),
        ),
        "deliveries_failed": _purge(
            db,
            NotificationDelivery,
            NotificationDelivery.status.in_([DeliveryStatus.DEAD, DeliveryStatus.FAILED])
            & (NotificationDelivery.updated_at < now - timedelta(days=cfg["failed_delivery_retention_days"])),
        ),
        "backup_runs": _purge(
            db,
            VmBackupRun,
            (VmBackupRun.status != "running")
            & (VmBackupRun.finished_at < now - timedelta(days=BACKUP_RUN_KEEP_DAYS)),
        ),
    }
    if any(counts.values()):
        logger.info("Alert retention removed %s", counts)
    return counts
