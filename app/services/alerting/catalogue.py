"""Alert type catalogue, settings overrides and global tuning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.alerting import AlertAudience, AlertSeverity, AlertTypeSetting
from app.models.commerce_account import SystemSetting

GLOBAL_SETTINGS_KEY = "alerting"


@dataclass(frozen=True)
class Finding:
    """One condition a detector found to be true right now."""

    alert_type: str
    resource_type: str
    resource_id: str
    resource_label: str
    title: str
    message: str = ""
    severity: Optional[str] = None
    details: dict = field(default_factory=dict)
    owner_user_id: Optional[int] = None

    @property
    def key(self) -> str:
        return f"{self.alert_type}:{self.resource_type}:{self.resource_id}"


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    default: int | float
    minimum: int | float = 0
    maximum: int | float = 10_000_000
    unit: str = ""

    def coerce(self, value: Any) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.key} must be a number")
        number = float(value) if isinstance(self.default, float) else int(value)
        if number < self.minimum or number > self.maximum:
            raise ValueError(f"{self.key} must be between {self.minimum} and {self.maximum}")
        return number


@dataclass(frozen=True)
class AlertTypeSpec:
    key: str
    title: str
    description: str
    severity: str
    resource_type: str
    params: tuple[ParamSpec, ...] = ()
    audience: str = AlertAudience.STAFF

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.params}


def _seconds(key: str, label: str, default: int, minimum: int = 30) -> ParamSpec:
    return ParamSpec(key, label, default, minimum=minimum, maximum=30 * 86400, unit="seconds")


def _minutes(key: str, label: str, default: int) -> ParamSpec:
    return ParamSpec(key, label, default, minimum=1, maximum=60 * 24 * 30, unit="minutes")


def _hours(key: str, label: str, default: int) -> ParamSpec:
    return ParamSpec(key, label, default, minimum=1, maximum=24 * 90, unit="hours")


_W = AlertSeverity.WARNING
_C = AlertSeverity.CRITICAL

_SPECS = (
    AlertTypeSpec(
        "storage_node_offline",
        "Storage node offline",
        "An enabled storage node has not contacted Rackflow.",
        _C,
        "storage_node",
        (_seconds("offline_after_seconds", "No contact for", 300),),
    ),
    AlertTypeSpec(
        "storage_node_ceph",
        "Storage node Ceph unhealthy",
        "An online storage node reports that CephFS is not healthy.",
        _C,
        "storage_node",
    ),
    AlertTypeSpec(
        "storage_node_error",
        "Storage node error",
        "An online storage node reports an error, or its SFTP gateway is down.",
        _W,
        "storage_node",
        (_seconds("offline_after_seconds", "Treat as offline after", 300),),
    ),
    AlertTypeSpec(
        "storage_tenant_error",
        "Storage tenant or share error",
        "A storage service or share is in an error state, or no node has served an active storage service while failover is active.",
        _W,
        "storage_tenant",
        (
            _seconds("min_age_seconds", "Error present for", 300),
            _seconds("unserved_after_seconds", "Not served by any node for", 120),
        ),
    ),
    AlertTypeSpec(
        "storage_tenant_failover",
        "Storage tenant on another node after a failover",
        "A storage service is served by a node other than its preferred node because of a failover.",
        _W,
        "storage_tenant",
    ),
    AlertTypeSpec(
        "storage_location_degraded",
        "Storage location degraded",
        "A location with at least two enabled storage nodes has fewer than two healthy ones.",
        _C,
        "storage_location",
    ),
    AlertTypeSpec(
        "proxy_runner_offline",
        "Proxy runner offline",
        "An enabled proxy runner has stopped phoning home.",
        _W,
        "proxy_runner",
        (_seconds("offline_after_seconds", "No contact for", 300),),
    ),
    AlertTypeSpec(
        "runner_offline",
        "Location runner offline",
        "An enabled DHCP, TFTP or media runner is not connected.",
        _C,
        "runner",
        (_seconds("offline_after_seconds", "No contact for", 300),),
    ),
    AlertTypeSpec(
        "pdu_poll_failed",
        "PDU not polling",
        "An enabled, metered PDU has a poll error or no recent successful poll.",
        _W,
        "pdu",
        (_seconds("stale_after_seconds", "No successful poll for", 600),),
    ),
    AlertTypeSpec(
        "power_feed_near_limit",
        "Power feed near its limit",
        "A rack power feed is drawing close to its usable limit.",
        _W,
        "power_feed",
        (ParamSpec("warn_percent", "Warn at this share of the usable limit", 90, 10, 100, "%"),),
    ),
    AlertTypeSpec(
        "power_feed_over_limit",
        "Power feed over its limit",
        "A rack power feed is drawing more than its usable limit.",
        _C,
        "power_feed",
    ),
    AlertTypeSpec(
        "switch_unreachable",
        "Switch not reporting",
        "An enabled switch has no recent port status from the SNMP poller.",
        _C,
        "switch",
        (_seconds("stale_after_seconds", "No port status for", 900),),
    ),
    AlertTypeSpec(
        "switch_vpc_inconsistent",
        "Switch vPC pairing inconsistent",
        "Stored vPC pairing data disagrees between peers, or vPC detection failed.",
        _W,
        "switch",
    ),
    AlertTypeSpec(
        "deployment_job_failed",
        "VM deployment job failed",
        "The latest deployment job of a service failed.",
        _W,
        "deployment_job",
        (_hours("lookback_hours", "Report failures from the last", 24),),
    ),
    AlertTypeSpec(
        "deployment_job_stuck",
        "VM deployment job stuck",
        "A deployment job is queued, retrying, waiting or running for too long.",
        _W,
        "deployment_job",
        (
            _minutes("queued_after_minutes", "Queued (no worker) for", 15),
            _minutes("retrying_after_minutes", "Retrying a failing step for", 30),
            _hours("waiting_after_hours", "Waiting for", 6),
            _minutes("running_after_minutes", "Running without a lease renewal for", 30),
        ),
    ),
    AlertTypeSpec(
        "boot_task_stuck",
        "Boot task stuck",
        "A boot task has been pending without the server fetching its boot script.",
        _W,
        "boot_task",
        (
            _minutes("pending_after_minutes", "Pending for", 30),
            _hours("max_age_hours", "Ignore tasks older than", 72),
        ),
    ),
    AlertTypeSpec(
        "install_failed",
        "OS install failed",
        "The latest install task of a server failed.",
        _W,
        "install_task",
        (_hours("lookback_hours", "Report failures from the last", 24),),
    ),
    AlertTypeSpec(
        "install_stuck",
        "OS install stuck",
        "An install task has been in progress for too long.",
        _W,
        "install_task",
        (_minutes("stuck_after_minutes", "In progress for", 120),),
    ),
    AlertTypeSpec(
        "private_network_error",
        "Private network attachment error",
        "A private network attachment is in error, or a detach keeps failing.",
        _W,
        "private_network_attachment",
        (_minutes("min_age_minutes", "Attach still pending after", 10),),
    ),
    AlertTypeSpec(
        "vm_backup_failed",
        "VM backup failed",
        "A VM backup started through Rackflow failed.",
        _W,
        "service",
        (_hours("lookback_hours", "Report failures from the last", 24),),
    ),
    AlertTypeSpec(
        "notification_delivery_failing",
        "Notification delivery failing",
        "Emails or alert notices are failing or not being sent.",
        _W,
        "system",
        (
            ParamSpec("failed_threshold", "Failures that raise the alert", 3, 1, 10_000),
            _minutes("window_minutes", "Failure window", 60),
            _minutes("queued_after_minutes", "Queued and unsent for", 30),
        ),
    ),
    AlertTypeSpec(
        "client_storage_quota",
        "Client storage near quota",
        "A client's storage share has passed the quota warning level.",
        AlertSeverity.INFO,
        "storage_share",
        (ParamSpec("quota_percent", "Warn at this share of the quota", 90, 50, 100, "%"),),
        audience=AlertAudience.CLIENT,
    ),
)

CATALOGUE: dict[str, AlertTypeSpec] = {spec.key: spec for spec in _SPECS}
STAFF_TYPES = tuple(s.key for s in _SPECS if s.audience == AlertAudience.STAFF)

GLOBAL_DEFAULTS: dict[str, int] = {
    "hold_down_seconds": 300,
    "reminder_interval_seconds": 0,
    "reopen_window_seconds": 1800,
    "flap_limit": 3,
    "storm_threshold": 5,
    "alert_retention_days": 90,
    "delivery_retention_days": 30,
    "failed_delivery_retention_days": 90,
    "client_rate_limit_per_hour": 20,
}

_GLOBAL_BOUNDS: dict[str, tuple[int, int]] = {
    "hold_down_seconds": (0, 86400),
    "reminder_interval_seconds": (0, 30 * 86400),
    "reopen_window_seconds": (0, 7 * 86400),
    "flap_limit": (1, 100),
    "storm_threshold": (1, 1000),
    "alert_retention_days": (1, 3650),
    "delivery_retention_days": (1, 3650),
    "failed_delivery_retention_days": (1, 3650),
    "client_rate_limit_per_hour": (1, 1000),
}


@dataclass(frozen=True)
class EffectiveSetting:
    spec: AlertTypeSpec
    enabled: bool
    severity: str
    params: dict
    reminder_interval_seconds: int
    hold_down_seconds: int
    overridden: bool


def get_global_settings(db: Session) -> dict[str, int]:
    row = db.get(SystemSetting, GLOBAL_SETTINGS_KEY)
    stored = row.value if row is not None and isinstance(row.value, dict) else {}
    merged = dict(GLOBAL_DEFAULTS)
    for key, (low, high) in _GLOBAL_BOUNDS.items():
        value = stored.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            merged[key] = value
    return merged


def update_global_settings(db: Session, changes: dict[str, Any]) -> dict[str, int]:
    current = get_global_settings(db)
    for key, value in changes.items():
        if key not in GLOBAL_DEFAULTS:
            raise ValueError(f"Unknown setting: {key}")
        low, high = _GLOBAL_BOUNDS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be a whole number between {low} and {high}")
        current[key] = value
    row = db.get(SystemSetting, GLOBAL_SETTINGS_KEY)
    if row is None:
        db.add(SystemSetting(key=GLOBAL_SETTINGS_KEY, value=current))
    else:
        row.value = dict(current)
    db.flush()
    return current


def effective_settings(db: Session) -> dict[str, EffectiveSetting]:
    overrides = {row.alert_type: row for row in db.query(AlertTypeSetting).all()}
    globals_ = get_global_settings(db)
    out: dict[str, EffectiveSetting] = {}
    for key, spec in CATALOGUE.items():
        row = overrides.get(key)
        params = spec.defaults()
        if row is not None and isinstance(row.params, dict):
            for param in spec.params:
                if param.key in row.params:
                    try:
                        params[param.key] = param.coerce(row.params[param.key])
                    except ValueError:
                        pass
        severity = row.severity if row is not None and row.severity in AlertSeverity.ALL else spec.severity
        reminder = (
            row.reminder_interval_seconds
            if row is not None and row.reminder_interval_seconds is not None
            else globals_["reminder_interval_seconds"]
        )
        hold_down = (
            row.hold_down_seconds
            if row is not None and row.hold_down_seconds is not None
            else globals_["hold_down_seconds"]
        )
        out[key] = EffectiveSetting(
            spec=spec,
            enabled=True if row is None else bool(row.enabled),
            severity=severity,
            params=params,
            reminder_interval_seconds=int(reminder),
            hold_down_seconds=int(hold_down),
            overridden=row is not None,
        )
    return out


def update_type_setting(
    db: Session,
    alert_type: str,
    *,
    enabled: Optional[bool] = None,
    severity: Optional[str] = None,
    params: Optional[dict] = None,
    reminder_interval_seconds: Optional[int] = None,
    hold_down_seconds: Optional[int] = None,
    reset: bool = False,
) -> AlertTypeSetting | None:
    spec = CATALOGUE.get(alert_type)
    if spec is None:
        raise KeyError(alert_type)
    row = db.get(AlertTypeSetting, alert_type)
    if reset:
        if row is not None:
            db.delete(row)
            db.flush()
        return None
    if severity is not None and severity not in AlertSeverity.ALL:
        raise ValueError("severity must be info, warning or critical")
    for name, value in (
        ("reminder_interval_seconds", reminder_interval_seconds),
        ("hold_down_seconds", hold_down_seconds),
    ):
        if value is not None and (isinstance(value, bool) or not 0 <= value <= 30 * 86400):
            raise ValueError(f"{name} must be between 0 and {30 * 86400}")
    if row is None:
        row = AlertTypeSetting(alert_type=alert_type, enabled=True, params={})
        db.add(row)
    if enabled is not None:
        row.enabled = enabled
    if severity is not None:
        row.severity = severity
    if reminder_interval_seconds is not None:
        row.reminder_interval_seconds = reminder_interval_seconds
    if hold_down_seconds is not None:
        row.hold_down_seconds = hold_down_seconds
    if params is not None:
        known = {p.key: p for p in spec.params}
        unknown = set(params) - set(known)
        if unknown:
            raise ValueError(f"Unknown parameter: {', '.join(sorted(unknown))}")
        merged = dict(row.params or {})
        for key, value in params.items():
            merged[key] = known[key].coerce(value)
        row.params = merged
    db.flush()
    return row


def utcnow() -> datetime:
    """Naive UTC now, the form every database datetime here is compared in."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
