"""Fan alert notices out to channels and staff recipients as outbox rows."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.alerting import (
    AlertAudience,
    AlertChannel,
    AlertRecipient,
    AlertSeverity,
    ChannelKind,
    DeliveryStatus,
    NotificationDelivery,
)
from app.models.user import User
from app.services.alerting import render
from app.services.alerting.catalogue import get_global_settings
from app.services.alerting.engine import CycleResult, Notice, severity_rank

logger = logging.getLogger(__name__)

_EMAIL_MAX = 320


@dataclass
class Target:
    kind: str
    key: str
    label: str
    address: str
    min_severity: str
    alert_types: list[str] = field(default_factory=list)
    send_resolved: bool = True
    channel_id: Optional[int] = None
    user_id: Optional[int] = None

    def wants(self, notice: Notice) -> bool:
        alert = notice.alert
        if severity_rank(alert.severity) < severity_rank(self.min_severity):
            return False
        if self.alert_types and alert.alert_type not in self.alert_types:
            return False
        if notice.event == "resolved" and not self.send_resolved:
            return False
        return True


def _valid_email(address: object) -> bool:
    return (
        isinstance(address, str)
        and 3 <= len(address) <= _EMAIL_MAX
        and address.count("@") == 1
        and " " not in address
        and not address.startswith("@")
        and not address.endswith("@")
    )


def build_targets(db: Session) -> list[Target]:
    targets: list[Target] = []
    for channel in db.query(AlertChannel).filter(AlertChannel.enabled.is_(True)).order_by(AlertChannel.id).all():
        types = [t for t in (channel.alert_types or []) if isinstance(t, str)]
        common = dict(
            kind=channel.kind,
            label=channel.name,
            min_severity=channel.min_severity if channel.min_severity in AlertSeverity.ALL else AlertSeverity.WARNING,
            alert_types=types,
            send_resolved=bool(channel.send_resolved),
            channel_id=channel.id,
        )
        if channel.kind == ChannelKind.EMAIL:
            addresses = (channel.config or {}).get("addresses") or []
            for address in dict.fromkeys(a.strip() for a in addresses if isinstance(a, str)):
                if _valid_email(address):
                    targets.append(Target(key=f"c{channel.id}:{address}", address=address, **common))
        else:
            targets.append(Target(key=f"c{channel.id}", address=channel.name, **common))
    recipients = (
        db.query(AlertRecipient, User)
        .join(User, User.id == AlertRecipient.user_id)
        .filter(AlertRecipient.enabled.is_(True), User.is_admin.is_(True))
        .order_by(AlertRecipient.id)
        .all()
    )
    for recipient, user in recipients:
        if not _valid_email(user.email):
            continue
        min_sev = recipient.min_severity if recipient.min_severity in AlertSeverity.ALL else AlertSeverity.WARNING
        targets.append(
            Target(
                kind=ChannelKind.EMAIL,
                key=f"u{user.id}",
                label=user.username,
                address=user.email,
                min_severity=min_sev,
                user_id=user.id,
            )
        )
    return targets


def _add_delivery(
    db: Session,
    target: Target,
    notices: list[Notice],
    now: datetime,
    *,
    summary: bool,
) -> None:
    items = [render.snapshot(n.alert, n.event) for n in notices]
    sent_at = now.isoformat() + "Z"
    payload = render.channel_payload(target.kind, items, sent_at=sent_at, summary=summary)
    first = notices[0]
    if summary:
        key = f"s:{sent_at}:{target.key}"
        event = "summary"
        alert_id = None
    else:
        key = f"a{first.alert.id}:{first.seq}:{first.event}:{target.key}"
        event = first.event
        alert_id = first.alert.id
    db.add(
        NotificationDelivery(
            idempotency_key=key,
            audience=AlertAudience.STAFF,
            alert_id=alert_id,
            user_id=target.user_id,
            channel_id=target.channel_id,
            channel_kind=target.kind,
            target=target.address[:512],
            event=event,
            payload=payload,
            status=DeliveryStatus.QUEUED,
            attempts=0,
            next_attempt_at=now,
        )
    )


def flush_notices(db: Session, result: CycleResult, now: datetime) -> int:
    """Queue outbox rows for this cycle's notices. Returns the number of rows added."""
    staff = [n for n in result.notices if n.alert.audience == AlertAudience.STAFF]
    client = [n for n in result.notices if n.alert.audience == AlertAudience.CLIENT]
    if client:
        from app.services.client_notifications import queue_alert_notices

        queue_alert_notices(db, client, now)
    if not staff:
        return 0
    storm = get_global_settings(db)["storm_threshold"]
    added = 0
    for target in build_targets(db):
        wanted = [n for n in staff if target.wants(n)]
        if not wanted:
            continue
        if len(wanted) > storm:
            _add_delivery(db, target, wanted, now, summary=True)
            added += 1
            continue
        for notice in wanted:
            _add_delivery(db, target, [notice], now, summary=False)
            added += 1
    db.flush()
    return added
