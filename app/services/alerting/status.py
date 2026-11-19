"""Last evaluation record, shown in the admin UI so a stopped worker is visible."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.commerce_account import SystemSetting
from app.services.alerting.catalogue import naive

STATUS_KEY = "alerting_status"


def write(db: Session, *, now: datetime, duration_ms: int, counts: dict[str, Any], failed_types: list[str]) -> None:
    value = {
        "last_run_at": naive(now).isoformat() + "Z",
        "duration_ms": duration_ms,
        "failed_types": failed_types,
        **counts,
    }
    row = db.get(SystemSetting, STATUS_KEY)
    if row is None:
        db.add(SystemSetting(key=STATUS_KEY, value=value))
    else:
        row.value = value
    db.commit()


def read(db: Session) -> dict[str, Any]:
    row = db.get(SystemSetting, STATUS_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}
