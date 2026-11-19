"""Staff-facing operations on alerts, silences, channels and recipients.

Shared by the admin API and the staff MCP tool so both behave the same.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.alerting import (
    Alert,
    AlertAudience,
    AlertChannel,
    AlertRecipient,
    AlertSeverity,
    AlertSilence,
    AlertState,
    ChannelKind,
    NotificationDelivery,
)
from app.models.user import User
from app.services.alerting import render, safe_http, status
from app.services.alerting.catalogue import CATALOGUE, naive, utcnow
from app.services.alerting.engine import active_silences, is_silenced
from app.services.alerting.secrets import mask_url, seal, unseal

MAX_SILENCE = timedelta(days=90)
RESOURCE_TYPES = frozenset({s.resource_type for s in CATALOGUE.values()} | {"system"})
MAX_PAGE = 200


class AlertError(ValueError):
    """A request the caller can fix; the message is safe to show."""


class NotFound(LookupError):
    pass


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = naive(value)
    return value.isoformat() + "Z" if value else None


# ------------------------------------------------------------------ alerts


def serialise_alert(alert: Alert, silences: list[AlertSilence]) -> dict[str, Any]:
    return {
        "id": alert.id,
        "type": alert.alert_type,
        "severity": alert.severity,
        "state": alert.state,
        "title": alert.title,
        "message": alert.message,
        "resource_type": alert.resource_type,
        "resource_id": alert.resource_id,
        "resource_label": alert.resource_label,
        "details": alert.details or {},
        "opened_at": _iso(alert.opened_at),
        "last_seen_at": _iso(alert.last_seen_at),
        "resolved_at": _iso(alert.resolved_at),
        "notified_at": _iso(alert.notified_at),
        "notify_count": alert.notify_count,
        "flap_count": alert.flap_count,
        "acknowledged": alert.acknowledged_at is not None,
        "acknowledged_at": _iso(alert.acknowledged_at),
        "acknowledged_by": alert.acknowledged_by_user.username if alert.acknowledged_by_user else None,
        "acknowledge_note": alert.acknowledge_note,
        "silenced": alert.state == AlertState.OPEN and is_silenced(alert, silences),
    }


def list_alerts(
    db: Session,
    *,
    state: str = "open",
    severity: Optional[str] = None,
    alert_type: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    acknowledged: Optional[bool] = None,
    query: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    if state not in {"open", "resolved", "all"}:
        raise AlertError("state must be open, resolved or all")
    if severity is not None and severity not in AlertSeverity.ALL:
        raise AlertError("severity must be info, warning or critical")
    q = db.query(Alert).filter(Alert.audience == AlertAudience.STAFF)
    if state != "all":
        q = q.filter(Alert.state == state)
    if severity:
        q = q.filter(Alert.severity == severity)
    if alert_type:
        q = q.filter(Alert.alert_type == alert_type)
    if resource_type:
        q = q.filter(Alert.resource_type == resource_type)
    if resource_id:
        q = q.filter(Alert.resource_id == str(resource_id))
    if acknowledged is True:
        q = q.filter(Alert.acknowledged_at.isnot(None))
    elif acknowledged is False:
        q = q.filter(Alert.acknowledged_at.is_(None))
    if query:
        like = f"%{query.strip()[:100]}%"
        q = q.filter(Alert.title.like(like) | Alert.resource_label.like(like) | Alert.message.like(like))
    total = q.count()
    order = (Alert.opened_at.desc(), Alert.id.desc()) if state != "resolved" else (Alert.resolved_at.desc(), Alert.id.desc())
    rows = q.order_by(*order).offset(max(0, offset)).limit(max(1, min(limit, MAX_PAGE))).all()
    silences = active_silences(db, naive(now) or utcnow())
    return {"total": total, "items": [serialise_alert(a, silences) for a in rows]}


def get_alert(db: Session, alert_id: int) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None or alert.audience != AlertAudience.STAFF:
        raise NotFound("Alert not found")
    return alert


def alert_detail(db: Session, alert_id: int) -> dict[str, Any]:
    alert = get_alert(db, alert_id)
    data = serialise_alert(alert, active_silences(db, utcnow()))
    deliveries = (
        db.query(NotificationDelivery)
        .filter(NotificationDelivery.alert_id == alert.id)
        .order_by(NotificationDelivery.id.desc())
        .limit(20)
        .all()
    )
    data["deliveries"] = [serialise_delivery(d) for d in deliveries]
    return data


def acknowledge(db: Session, alert_id: int, user_id: int, note: Optional[str] = None) -> Alert:
    alert = get_alert(db, alert_id)
    if alert.state != AlertState.OPEN:
        raise AlertError("Only an open alert can be acknowledged")
    alert.acknowledged_at = utcnow()
    alert.acknowledged_by = user_id
    alert.acknowledge_note = (note or "").strip()[:512] or None
    db.commit()
    return alert


def unacknowledge(db: Session, alert_id: int) -> Alert:
    alert = get_alert(db, alert_id)
    alert.acknowledged_at = None
    alert.acknowledged_by = None
    alert.acknowledge_note = None
    db.commit()
    return alert


def summary(db: Session, now: Optional[datetime] = None) -> dict[str, Any]:
    """Counts for the navigation badge plus worker and channel health."""
    now = naive(now) or utcnow()
    silences = active_silences(db, now)
    open_alerts = (
        db.query(Alert)
        .filter(Alert.audience == AlertAudience.STAFF, Alert.state == AlertState.OPEN)
        .limit(5000)
        .all()
    )
    counts = {"critical": 0, "warning": 0, "info": 0}
    acknowledged = silenced = 0
    for alert in open_alerts:
        if alert.acknowledged_at is not None:
            acknowledged += 1
        elif is_silenced(alert, silences):
            silenced += 1
        else:
            counts[alert.severity] = counts.get(alert.severity, 0) + 1
    st = status.read(db)
    last_run = naive(datetime.fromisoformat(st["last_run_at"].rstrip("Z"))) if st.get("last_run_at") else None
    stale = last_run is None or (now - last_run).total_seconds() > max(300, settings.alert_worker_interval_seconds * 5)
    channels = db.query(func.count(AlertChannel.id)).filter(AlertChannel.enabled.is_(True)).scalar() or 0
    recipients = db.query(func.count(AlertRecipient.id)).filter(AlertRecipient.enabled.is_(True)).scalar() or 0
    return {
        "active": sum(counts.values()),
        "by_severity": counts,
        "acknowledged": acknowledged,
        "silenced": silenced,
        "last_evaluated_at": st.get("last_run_at"),
        "evaluation_stale": stale,
        "failed_types": st.get("failed_types", []),
        "smtp_enabled": bool(settings.smtp_enabled),
        "channels": channels,
        "recipients": recipients,
        "has_destination": bool(channels or recipients),
    }


# ------------------------------------------------------------------ silences


def serialise_silence(s: AlertSilence, now: datetime) -> dict[str, Any]:
    return {
        "id": s.id,
        "alert_type": s.alert_type,
        "resource_type": s.resource_type,
        "resource_id": s.resource_id,
        "starts_at": _iso(s.starts_at),
        "ends_at": _iso(s.ends_at),
        "reason": s.reason,
        "created_by": s.created_by_user.username if s.created_by_user else None,
        "active": naive(s.starts_at) <= now < naive(s.ends_at),
    }


def create_silence(
    db: Session,
    *,
    user_id: Optional[int],
    alert_type: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    ends_at: Optional[datetime] = None,
    reason: Optional[str] = None,
) -> AlertSilence:
    now = utcnow()
    if alert_type and alert_type not in CATALOGUE:
        raise AlertError("Unknown alert type")
    if resource_type and resource_type not in RESOURCE_TYPES:
        raise AlertError("Unknown resource type")
    if resource_id and not resource_type:
        raise AlertError("A resource id needs a resource type")
    if not alert_type and not resource_type:
        raise AlertError("Silence an alert type, a resource, or both")
    if ends_at is not None:
        end = naive(ends_at)
    elif duration_minutes:
        end = now + timedelta(minutes=int(duration_minutes))
    else:
        raise AlertError("Give a duration or an end time")
    if end <= now:
        raise AlertError("The silence must end in the future")
    if end - now > MAX_SILENCE:
        raise AlertError("A silence can last at most 90 days")
    row = AlertSilence(
        alert_type=alert_type or None,
        resource_type=resource_type or None,
        resource_id=str(resource_id) if resource_id else None,
        starts_at=now,
        ends_at=end,
        reason=(reason or "").strip()[:512] or None,
        created_by=user_id,
    )
    db.add(row)
    db.commit()
    return row


def silence_alert(db: Session, alert_id: int, **kwargs: Any) -> AlertSilence:
    alert = get_alert(db, alert_id)
    return create_silence(
        db,
        alert_type=alert.alert_type,
        resource_type=alert.resource_type,
        resource_id=alert.resource_id,
        **kwargs,
    )


def list_silences(db: Session, *, include_expired: bool = False) -> list[dict[str, Any]]:
    now = utcnow()
    q = db.query(AlertSilence)
    if not include_expired:
        q = q.filter(AlertSilence.ends_at > now)
    return [serialise_silence(s, now) for s in q.order_by(AlertSilence.ends_at.desc()).limit(500).all()]


def delete_silence(db: Session, silence_id: int) -> None:
    row = db.get(AlertSilence, silence_id)
    if row is None:
        raise NotFound("Silence not found")
    db.delete(row)
    db.commit()


# ------------------------------------------------------------------ channels


def serialise_channel(channel: AlertChannel) -> dict[str, Any]:
    cfg = channel.config or {}
    return {
        "id": channel.id,
        "name": channel.name,
        "kind": channel.kind,
        "enabled": channel.enabled,
        "min_severity": channel.min_severity,
        "alert_types": list(channel.alert_types or []),
        "send_resolved": channel.send_resolved,
        "addresses": list(cfg.get("addresses") or []),
        "url": mask_url(unseal(cfg.get("url"))),
        "has_secret": bool(cfg.get("secret")),
    }


def _check_channel_fields(kind: str, min_severity: str, alert_types: list[str]) -> None:
    if kind not in ChannelKind.ALL:
        raise AlertError("kind must be email, webhook, discord or slack")
    if min_severity not in AlertSeverity.ALL:
        raise AlertError("min_severity must be info, warning or critical")
    unknown = [t for t in alert_types if t not in CATALOGUE]
    if unknown:
        raise AlertError(f"Unknown alert type: {unknown[0]}")


def _check_url(url: str) -> str:
    url = (url or "").strip()
    try:
        safe_http.validate_url(url, public_only=False, https_only=False, resolver=lambda host, port: ["127.0.0.1"])
    except safe_http.WebhookUrlError as exc:
        raise AlertError(str(exc)) from exc
    return url


def _check_addresses(addresses: list[str]) -> list[str]:
    from app.services.alerting.dispatch import _valid_email

    cleaned = list(dict.fromkeys(a.strip() for a in addresses if a and a.strip()))
    if not cleaned:
        raise AlertError("Add at least one email address")
    if len(cleaned) > 25:
        raise AlertError("At most 25 addresses per channel")
    for address in cleaned:
        if not _valid_email(address):
            raise AlertError(f"Not a valid email address: {address}")
    return cleaned


def create_channel(
    db: Session,
    *,
    name: str,
    kind: str,
    user_id: Optional[int],
    enabled: bool = True,
    min_severity: str = AlertSeverity.WARNING,
    alert_types: Optional[list[str]] = None,
    send_resolved: bool = True,
    addresses: Optional[list[str]] = None,
    url: Optional[str] = None,
    secret: Optional[str] = None,
) -> AlertChannel:
    name = (name or "").strip()
    if not name or len(name) > 128:
        raise AlertError("Give the channel a name of up to 128 characters")
    types = list(alert_types or [])
    _check_channel_fields(kind, min_severity, types)
    if db.query(AlertChannel).filter(AlertChannel.name == name).first():
        raise AlertError("A channel with that name already exists")
    channel = AlertChannel(
        name=name,
        kind=kind,
        enabled=enabled,
        min_severity=min_severity,
        alert_types=types,
        send_resolved=send_resolved,
        config={},
        created_by=user_id,
    )
    channel.config = _config_for(kind, {}, addresses=addresses, url=url, secret=secret, creating=True)
    db.add(channel)
    db.commit()
    return channel


def _config_for(
    kind: str,
    current: dict,
    *,
    addresses: Optional[list[str]],
    url: Optional[str],
    secret: Optional[str],
    creating: bool = False,
) -> dict:
    cfg = dict(current)
    if kind == ChannelKind.EMAIL:
        if addresses is not None or creating:
            cfg = {"addresses": _check_addresses(addresses or [])}
        return cfg
    if url is not None or creating:
        cfg["url"] = seal(_check_url(url or ""))
    if kind == ChannelKind.WEBHOOK:
        if secret is not None:
            if secret:
                cfg["secret"] = seal(secret.strip()[:200])
            else:
                cfg.pop("secret", None)
    return cfg


def update_channel(db: Session, channel_id: int, **fields: Any) -> AlertChannel:
    channel = db.get(AlertChannel, channel_id)
    if channel is None:
        raise NotFound("Channel not found")
    name = fields.get("name")
    if name is not None:
        name = name.strip()
        if not name or len(name) > 128:
            raise AlertError("Give the channel a name of up to 128 characters")
        clash = db.query(AlertChannel).filter(AlertChannel.name == name, AlertChannel.id != channel.id).first()
        if clash:
            raise AlertError("A channel with that name already exists")
        channel.name = name
    min_severity = fields.get("min_severity", channel.min_severity)
    alert_types = fields.get("alert_types", list(channel.alert_types or []))
    _check_channel_fields(channel.kind, min_severity, alert_types)
    channel.min_severity = min_severity
    channel.alert_types = list(alert_types)
    for key in ("enabled", "send_resolved"):
        if fields.get(key) is not None:
            setattr(channel, key, bool(fields[key]))
    channel.config = _config_for(
        channel.kind,
        channel.config or {},
        addresses=fields.get("addresses"),
        url=fields.get("url"),
        secret=fields.get("secret"),
    )
    db.commit()
    return channel


def delete_channel(db: Session, channel_id: int) -> None:
    channel = db.get(AlertChannel, channel_id)
    if channel is None:
        raise NotFound("Channel not found")
    db.delete(channel)
    db.commit()


def send_test_notice(db: Session, channel_id: int, sender: Any = None) -> dict[str, Any]:
    """Send a sample notice straight away and report the result."""
    from app.services.alerting.sender import DeliverySender

    channel = db.get(AlertChannel, channel_id)
    if channel is None:
        raise NotFound("Channel not found")
    now = utcnow()
    item = {
        "id": 0,
        "event": "test",
        "type": "test",
        "severity": "info",
        "title": "Test notification from Rackflow",
        "message": f"This is a test of the alert channel '{channel.name}'. No action is needed.",
        "resource_type": "system",
        "resource_id": "test",
        "resource_label": "",
        "opened_at": now.isoformat() + "Z",
        "resolved_at": None,
        "notify_count": 0,
        "details": {},
    }
    payload = render.channel_payload(channel.kind, [item], sent_at=now.isoformat() + "Z", summary=False)
    sender = sender or DeliverySender()
    targets = [channel.name]
    if channel.kind == ChannelKind.EMAIL:
        targets = list((channel.config or {}).get("addresses") or [])
    errors: list[str] = []
    for target in targets:
        row = NotificationDelivery(
            idempotency_key=f"test:{channel.id}:{now.isoformat()}:{target}",
            audience=AlertAudience.STAFF,
            channel_id=channel.id,
            channel_kind=channel.kind,
            target=target,
            event="test",
            payload=payload,
            attempts=1,
        )
        try:
            sender._deliver(db, row)
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            from app.services.alerting.scrub import scrub

            errors.append(f"{target}: {type(exc).__name__}: {scrub(exc, 160)}")
    return {"ok": not errors, "errors": errors}


# ------------------------------------------------------------------ recipients


def list_recipients(db: Session) -> list[dict[str, Any]]:
    admins = db.query(User).filter(User.is_admin.is_(True)).order_by(User.username).all()
    by_user = {r.user_id: r for r in db.query(AlertRecipient).all()}
    out = []
    for user in admins:
        row = by_user.get(user.id)
        out.append(
            {
                "user_id": user.id,
                "username": user.username,
                "email": user.email,
                "enabled": bool(row and row.enabled),
                "min_severity": row.min_severity if row else AlertSeverity.WARNING,
            }
        )
    return out


def set_recipient(db: Session, user_id: int, *, enabled: bool, min_severity: str) -> None:
    if min_severity not in AlertSeverity.ALL:
        raise AlertError("min_severity must be info, warning or critical")
    user = db.get(User, user_id)
    if user is None or not user.is_admin:
        raise NotFound("Staff user not found")
    if enabled and not user.email:
        raise AlertError("That user has no email address")
    row = db.query(AlertRecipient).filter(AlertRecipient.user_id == user_id).first()
    if row is None:
        row = AlertRecipient(user_id=user_id)
        db.add(row)
    row.enabled = enabled
    row.min_severity = min_severity
    db.commit()


# ------------------------------------------------------------------ deliveries


def serialise_delivery(d: NotificationDelivery) -> dict[str, Any]:
    return {
        "id": d.id,
        "alert_id": d.alert_id,
        "channel_id": d.channel_id,
        "channel_kind": d.channel_kind,
        "target": d.target,
        "event": d.event,
        "status": d.status,
        "attempts": d.attempts,
        "error": d.error,
        "created_at": _iso(d.created_at),
        "sent_at": _iso(d.sent_at),
        "next_attempt_at": _iso(d.next_attempt_at),
    }


def list_deliveries(db: Session, *, status_filter: Optional[str] = None, limit: int = 50, offset: int = 0) -> dict:
    q = db.query(NotificationDelivery).filter(NotificationDelivery.audience == AlertAudience.STAFF)
    if status_filter:
        q = q.filter(NotificationDelivery.status == status_filter)
    total = q.count()
    rows = q.order_by(NotificationDelivery.id.desc()).offset(max(0, offset)).limit(max(1, min(limit, MAX_PAGE))).all()
    return {"total": total, "items": [serialise_delivery(d) for d in rows]}
