"""Deliver queued notification rows: claim, send, retry with backoff, give up when dead."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.models.alerting import (
    AlertAudience,
    AlertChannel,
    ChannelKind,
    ClientNotificationPref,
    DeliveryStatus,
    NotificationDelivery,
)
from app.services.alerting import safe_http
from app.services.alerting.catalogue import utcnow
from app.services.alerting.scrub import scrub
from app.services.alerting.secrets import unseal
from app.services.email_sender import deliver as smtp_deliver

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 408, 425}


class PermanentFailure(Exception):
    """The delivery cannot succeed by retrying."""


class SkipDelivery(Exception):
    """Nothing should be sent (for example the channel was switched off)."""


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class DeliverySender:
    def __init__(
        self,
        *,
        config: Settings = settings,
        email_sender: Optional[Callable[..., None]] = None,
        http_post: Optional[Callable[..., safe_http.HttpResult]] = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.email_sender = email_sender or (lambda **kw: smtp_deliver(self.config, **kw))
        self.http_post = http_post or safe_http.post
        self.clock = clock

    @staticmethod
    def _locked(statement, db: Session):
        if db.get_bind().dialect.name in {"mysql", "postgresql"}:
            return statement.with_for_update(skip_locked=True)
        return statement.with_for_update()

    def _claim(self, db: Session, owner: str, batch_size: int) -> list[int]:
        now = self.clock()
        due = or_(NotificationDelivery.next_attempt_at.is_(None), NotificationDelivery.next_attempt_at <= now)
        available = or_(
            and_(
                NotificationDelivery.status.in_([DeliveryStatus.QUEUED, DeliveryStatus.FAILED]),
                due,
            ),
            and_(
                NotificationDelivery.status == DeliveryStatus.SENDING,
                NotificationDelivery.claim_expires_at <= now,
            ),
        )
        statement = (
            select(NotificationDelivery).where(available).order_by(NotificationDelivery.id).limit(batch_size)
        )
        rows = list(db.execute(self._locked(statement, db)).scalars())
        expires = now + timedelta(seconds=max(60, int(self.config.alert_delivery_timeout_seconds * 4)))
        for row in rows:
            row.status = DeliveryStatus.SENDING
            row.claim_token = owner
            row.claim_expires_at = expires
        db.commit()
        return [row.id for row in rows]

    def _send_email(self, row: NotificationDelivery) -> None:
        if not self.config.smtp_enabled:
            raise SkipDelivery("SMTP delivery is disabled")
        payload = row.payload or {}
        self.email_sender(
            to_address=row.target,
            subject=str(payload.get("subject", "Rackflow notification")),
            body_text=str(payload.get("text", "")),
            body_html=payload.get("html"),
        )

    def _webhook_target(self, db: Session, row: NotificationDelivery) -> tuple[str, Optional[str], bool]:
        """Return ``(url, signing secret, strict)``; strict means https and public addresses only."""
        if row.audience == AlertAudience.CLIENT:
            prefs = db.query(ClientNotificationPref).filter(ClientNotificationPref.user_id == row.user_id).first()
            url = unseal(prefs.webhook_url) if prefs is not None else None
            if not url:
                raise SkipDelivery("The client has no webhook configured")
            return url, unseal(prefs.webhook_secret), True
        channel = db.get(AlertChannel, row.channel_id) if row.channel_id else None
        if channel is None:
            raise PermanentFailure("The channel no longer exists")
        if not channel.enabled:
            raise SkipDelivery("The channel is switched off")
        cfg = channel.config or {}
        url = unseal(cfg.get("url"))
        if not url:
            raise PermanentFailure("The channel has no URL")
        return url, unseal(cfg.get("secret")), False

    def _send_webhook(self, db: Session, row: NotificationDelivery) -> None:
        url, secret, strict = self._webhook_target(db, row)
        body = json.dumps(row.payload or {}, separators=(",", ":"), sort_keys=True).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Rackflow-Alerts/1",
            "X-Rackflow-Event": row.event,
            "X-Rackflow-Delivery": row.idempotency_key,
        }
        if secret and row.channel_kind in (ChannelKind.WEBHOOK,):
            headers["X-Rackflow-Signature"] = sign(secret, body)
        try:
            result = self.http_post(
                url,
                body,
                headers,
                public_only=strict,
                https_only=strict,
                timeout=self.config.alert_delivery_timeout_seconds,
            )
        except safe_http.WebhookUrlError as exc:
            raise PermanentFailure(str(exc)) from exc
        if 200 <= result.status < 300:
            return
        message = f"The endpoint answered HTTP {result.status}"
        if 400 <= result.status < 500 and result.status not in RETRY_STATUSES:
            raise PermanentFailure(message)
        raise RuntimeError(message)

    def _deliver(self, db: Session, row: NotificationDelivery) -> None:
        if row.channel_kind == ChannelKind.EMAIL:
            self._send_email(row)
        else:
            self._send_webhook(db, row)

    def send_batch(self, db: Session, *, owner: str, batch_size: Optional[int] = None) -> int:
        size = batch_size or self.config.alert_delivery_batch_size
        ids = self._claim(db, owner, size)
        sent = 0
        for row_id in ids:
            row = db.get(NotificationDelivery, row_id)
            if row is None or row.claim_token != owner:
                continue
            row.attempts += 1
            try:
                self._deliver(db, row)
            except SkipDelivery as exc:
                row.status = DeliveryStatus.SKIPPED
                row.error = str(exc)
                row.next_attempt_at = None
            except PermanentFailure as exc:
                row.status = DeliveryStatus.DEAD
                row.error = scrub(exc, 200)
                row.next_attempt_at = None
                self._note_failure(db, row)
            except Exception as exc:  # noqa: BLE001 - any transport error is retried
                detail = scrub(exc, 160)
                row.error = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
                if row.attempts >= self.config.alert_delivery_max_attempts:
                    row.status = DeliveryStatus.DEAD
                    row.next_attempt_at = None
                    self._note_failure(db, row)
                else:
                    row.status = DeliveryStatus.FAILED
                    delay = min(3600, 60 * (2 ** min(row.attempts - 1, 6)))
                    row.next_attempt_at = self.clock() + timedelta(seconds=delay)
            else:
                row.status = DeliveryStatus.SENT
                row.sent_at = self.clock()
                row.error = None
                row.next_attempt_at = None
                sent += 1
            row.claim_token = None
            row.claim_expires_at = None
            db.commit()
        return sent

    @staticmethod
    def _note_failure(db: Session, row: NotificationDelivery) -> None:
        """Keep a short, client-safe failure marker on a client's preferences."""
        if row.audience != AlertAudience.CLIENT or row.channel_kind == ChannelKind.EMAIL:
            return
        prefs = db.query(ClientNotificationPref).filter(ClientNotificationPref.user_id == row.user_id).first()
        if prefs is not None:
            prefs.webhook_error = "The last webhook delivery failed"
