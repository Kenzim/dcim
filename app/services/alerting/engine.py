"""Alert lifecycle: reconcile detector findings with stored alerts.

``run_cycle`` evaluates every enabled alert type once, opens, refreshes and
resolves alerts, and queues notices in the outbox in the same transaction.
Notices are only queued here; the sender delivers them later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from app.models.alerting import (
    Alert,
    AlertAudience,
    AlertSeverity,
    AlertSilence,
    AlertState,
)
from app.services.alerting.catalogue import (
    CATALOGUE,
    EffectiveSetting,
    Finding,
    effective_settings,
    get_global_settings,
    naive,
    utcnow,
)
from app.services.alerting.detectors import DETECTORS, Detector

logger = logging.getLogger(__name__)

EVENT_OPENED = "opened"
EVENT_REMINDER = "reminder"
EVENT_ESCALATED = "escalated"
EVENT_RESOLVED = "resolved"
EVENT_FLAPPING = "flapping"


@dataclass
class Notice:
    alert: Alert
    event: str
    seq: int


@dataclass
class CycleResult:
    opened: int = 0
    reopened: int = 0
    resolved: int = 0
    refreshed: int = 0
    notices: list[Notice] = field(default_factory=list)
    failed_types: list[str] = field(default_factory=list)
    skipped_types: list[str] = field(default_factory=list)


def severity_rank(severity: str) -> int:
    return AlertSeverity.RANK.get(severity, 0)


def active_silences(db: Session, now: datetime) -> list[AlertSilence]:
    return (
        db.query(AlertSilence)
        .filter(AlertSilence.starts_at <= now, AlertSilence.ends_at > now)
        .all()
    )


def silence_matches(silence: AlertSilence, alert_type: str, resource_type: str, resource_id: str) -> bool:
    return (
        (silence.alert_type is None or silence.alert_type == alert_type)
        and (silence.resource_type is None or silence.resource_type == resource_type)
        and (silence.resource_id is None or silence.resource_id == str(resource_id))
    )


def is_silenced(alert: Alert, silences: Iterable[AlertSilence]) -> bool:
    return any(silence_matches(s, alert.alert_type, alert.resource_type, alert.resource_id) for s in silences)


class _Cycle:
    """State shared by the types reconciled in one pass."""

    def __init__(self, db: Session, now: datetime) -> None:
        self.db = db
        self.now = now
        self.globals = get_global_settings(db)
        self.silences = active_silences(db, now)
        self.result = CycleResult()

    # ---- notices -------------------------------------------------------

    def notify(self, alert: Alert, event: str) -> bool:
        """Queue a notice unless the alert is silenced. Returns whether one was queued."""
        if is_silenced(alert, self.silences):
            return False
        alert.notify_count = (alert.notify_count or 0) + 1
        alert.last_notified_at = self.now
        if alert.notified_at is None and event != EVENT_RESOLVED:
            alert.notified_at = self.now
        self.result.notices.append(Notice(alert, event, alert.notify_count))
        return True

    # ---- transitions ---------------------------------------------------

    def open_alert(self, finding: Finding, setting: EffectiveSetting, severity: str) -> Alert:
        db = self.db
        key = finding.key
        reopen_cutoff = self.now - timedelta(seconds=self.globals["reopen_window_seconds"])
        previous = (
            db.query(Alert)
            .filter(
                Alert.alert_type == finding.alert_type,
                Alert.resource_type == finding.resource_type,
                Alert.resource_id == finding.resource_id,
                Alert.state == AlertState.RESOLVED,
                Alert.resolved_at >= reopen_cutoff,
            )
            .order_by(Alert.resolved_at.desc())
            .first()
        )
        if previous is not None:
            alert = previous
            alert.state = AlertState.OPEN
            alert.active_key = key
            alert.resolved_at = None
            alert.clear_since = None
            alert.resolve_notice_due_at = None
            alert.resolve_notified = False
            alert.flap_count = (alert.flap_count or 0) + 1
            self._refresh_fields(alert, finding, severity)
            self.result.reopened += 1
            limit = self.globals["flap_limit"]
            if alert.flap_count < limit:
                self.notify(alert, EVENT_OPENED)
            elif alert.flap_count == limit:
                self.notify(alert, EVENT_FLAPPING)
            return alert

        alert = Alert(
            audience=CATALOGUE[finding.alert_type].audience,
            owner_user_id=finding.owner_user_id,
            alert_type=finding.alert_type,
            resource_type=finding.resource_type,
            resource_id=finding.resource_id,
            severity=severity,
            title=finding.title[:255],
            state=AlertState.OPEN,
            active_key=key,
            opened_at=self.now,
            last_seen_at=self.now,
            details={},
            notify_count=0,
            flap_count=0,
            resolve_notified=False,
        )
        self._refresh_fields(alert, finding, severity)
        db.add(alert)
        db.flush()
        self.result.opened += 1
        self.notify(alert, EVENT_OPENED)
        return alert

    def _refresh_fields(self, alert: Alert, finding: Finding, severity: str) -> None:
        alert.resource_label = (finding.resource_label or "")[:255] or None
        alert.title = finding.title[:255]
        alert.message = finding.message or None
        alert.details = dict(finding.details or {})
        alert.severity = severity
        alert.last_seen_at = self.now

    def refresh_open(self, alert: Alert, finding: Finding, setting: EffectiveSetting, severity: str) -> None:
        old_severity = alert.severity
        self._refresh_fields(alert, finding, severity)
        alert.clear_since = None
        self.result.refreshed += 1
        flap_limit = self.globals["flap_limit"]
        paused = (alert.flap_count or 0) >= flap_limit
        silenced = is_silenced(alert, self.silences)

        if alert.notified_at is None:
            # Opened while silenced (or while a flapping alert was suppressed).
            if not silenced and alert.acknowledged_at is None and not paused:
                self.notify(alert, EVENT_OPENED)
            return
        if silenced or paused:
            return
        if severity_rank(severity) > severity_rank(old_severity):
            self.notify(alert, EVENT_ESCALATED)
            return
        interval = setting.reminder_interval_seconds
        if interval > 0 and alert.acknowledged_at is None:
            last = naive(alert.last_notified_at) or naive(alert.notified_at)
            if last is not None and (self.now - last).total_seconds() >= interval:
                self.notify(alert, EVENT_REMINDER)

    def clear_open(self, alert: Alert, setting: Optional[EffectiveSetting], *, quiet: bool = False) -> None:
        """The condition is not present this cycle; resolve once the hold-down has passed."""
        hold = 0 if quiet or setting is None else setting.hold_down_seconds
        if alert.clear_since is None:
            alert.clear_since = self.now
        if (self.now - naive(alert.clear_since)).total_seconds() < hold:
            return
        self.resolve(alert, quiet=quiet)

    def resolve(self, alert: Alert, *, quiet: bool = False) -> None:
        alert.state = AlertState.RESOLVED
        alert.active_key = None
        alert.resolved_at = self.now
        alert.clear_since = None
        alert.acknowledged_at = None
        alert.acknowledged_by = None
        alert.acknowledge_note = None
        self.result.resolved += 1
        if quiet or alert.notified_at is None:
            alert.resolve_notified = True
            return
        if (alert.flap_count or 0) >= self.globals["flap_limit"]:
            alert.resolve_notice_due_at = self.now + timedelta(seconds=self.globals["reopen_window_seconds"])
            alert.resolve_notified = False
            return
        alert.resolve_notified = True
        self.notify(alert, EVENT_RESOLVED)

    def send_due_resolved(self) -> None:
        """Resolved notices held back for flapping alerts, once they have stayed resolved."""
        due = (
            self.db.query(Alert)
            .filter(
                Alert.state == AlertState.RESOLVED,
                Alert.resolve_notified.is_(False),
                Alert.resolve_notice_due_at.isnot(None),
                Alert.resolve_notice_due_at <= self.now,
            )
            .limit(500)
            .all()
        )
        for alert in due:
            alert.resolve_notified = True
            alert.resolve_notice_due_at = None
            if alert.notified_at is not None:
                self.notify(alert, EVENT_RESOLVED)


def _open_alerts(db: Session, alert_type: str) -> dict[str, Alert]:
    rows = db.query(Alert).filter(Alert.alert_type == alert_type, Alert.state == AlertState.OPEN).all()
    return {a.active_key or f"{a.alert_type}:{a.resource_type}:{a.resource_id}": a for a in rows}


def reconcile_type(
    cycle: _Cycle, alert_type: str, setting: EffectiveSetting, findings: list[Finding]
) -> None:
    open_alerts = _open_alerts(cycle.db, alert_type)
    seen: set[str] = set()
    for finding in findings:
        if finding.key in seen:
            continue
        seen.add(finding.key)
        severity = finding.severity or setting.severity
        alert = open_alerts.get(finding.key)
        if alert is None:
            cycle.open_alert(finding, setting, severity)
        else:
            cycle.refresh_open(alert, finding, setting, severity)
    for key, alert in open_alerts.items():
        if key not in seen:
            cycle.clear_open(alert, setting)


def close_disabled(cycle: _Cycle, alert_type: str) -> None:
    for alert in _open_alerts(cycle.db, alert_type).values():
        cycle.clear_open(alert, None, quiet=True)


def run_cycle(
    db: Session,
    *,
    now: Optional[datetime] = None,
    detectors: Optional[dict[str, Detector]] = None,
    only_types: Optional[Iterable[str]] = None,
    flush: Optional[Callable[[Session, CycleResult, datetime], None]] = None,
) -> CycleResult:
    """Evaluate alert types, update alerts and queue notices. Commits once at the end.

    Alert changes and their notices are written together, so a failure while
    queueing notices rolls the whole cycle back and the next cycle retries it.
    """
    now = naive(now) or utcnow()
    table = detectors if detectors is not None else DETECTORS
    wanted = set(only_types) if only_types is not None else None
    settings_by_type = effective_settings(db)
    cycle = _Cycle(db, now)

    for alert_type, setting in settings_by_type.items():
        if wanted is not None and alert_type not in wanted:
            continue
        detector = table.get(alert_type)
        if detector is None:
            cycle.result.skipped_types.append(alert_type)
            continue
        savepoint = db.begin_nested()
        try:
            if not setting.enabled:
                close_disabled(cycle, alert_type)
            else:
                findings = detector(db, setting.params, now)
                reconcile_type(cycle, alert_type, setting, findings)
            savepoint.commit()
        except Exception:  # noqa: BLE001 - one broken detector must not stop the others
            savepoint.rollback()
            logger.exception("Alert detector %s failed", alert_type)
            cycle.result.failed_types.append(alert_type)

    savepoint = db.begin_nested()
    try:
        cycle.send_due_resolved()
        savepoint.commit()
    except Exception:  # noqa: BLE001
        savepoint.rollback()
        logger.exception("Sending held resolved notices failed")

    if flush is None:
        from app.services.alerting.dispatch import flush_notices as flush
    flush(db, cycle.result, now)
    db.commit()
    return cycle.result
