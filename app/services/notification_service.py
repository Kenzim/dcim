"""Durable notification enqueueing and standard-library SMTP delivery."""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable, Optional, Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.models.reseller import (
    NotificationOutbox,
    NotificationOutboxStatus,
    Reseller,
)


class NotificationEvent:
    CYCLE_CHARGE_FAILED = "cycle_charge_failed"
    PAYMENT_ACTION_REQUIRED = "payment_action_required"
    GRACE_WARNING = "grace_warning"
    SERVICE_SUSPENDED = "service_suspended"
    PAYMENT_RECOVERED = "payment_recovered"
    TOPUP_RECEIVED = "topup_received"
    USDT_PENDING = "usdt_pending_confirmations"
    USDT_DETECTED = "usdt_detected"


_SUBJECTS = {
    NotificationEvent.CYCLE_CHARGE_FAILED: "Rackflow recurring payment failed",
    NotificationEvent.PAYMENT_ACTION_REQUIRED: "Rackflow payment action required",
    NotificationEvent.GRACE_WARNING: "Rackflow payment grace period warning",
    NotificationEvent.SERVICE_SUSPENDED: "Rackflow service suspended for nonpayment",
    NotificationEvent.PAYMENT_RECOVERED: "Rackflow payment recovered",
    NotificationEvent.TOPUP_RECEIVED: "Rackflow account top-up received",
    NotificationEvent.USDT_PENDING: "Rackflow USDT payment detected",
    NotificationEvent.USDT_DETECTED: "Rackflow USDT transfer detected",
}


def _money(data: dict) -> str:
    cents = int(data.get("amount_cents") or 0)
    currency = str(data.get("currency") or "USD").upper()
    return f"{currency} {cents / 100:.2f}"


def render_notification(event: str, data: dict) -> tuple[str, str]:
    """Render text-only messages from a small, secret-free data contract."""
    subject = _SUBJECTS.get(event, "Rackflow account notification")
    service = str(data.get("service_name") or "your service")
    invoice = str(data.get("invoice_number") or data.get("invoice_id") or "")
    grace = str(data.get("grace_until") or "the stated deadline")
    amount = _money(data)
    bodies = {
        NotificationEvent.CYCLE_CHARGE_FAILED: (
            f"The recurring charge of {amount} for {service} could not be "
            f"completed. Invoice {invoice} remains unpaid."
        ),
        NotificationEvent.PAYMENT_ACTION_REQUIRED: (
            f"Payment action is required for invoice {invoice} ({amount}) for "
            f"{service}. Sign in to the reseller panel to complete payment."
        ),
        NotificationEvent.GRACE_WARNING: (
            f"Invoice {invoice} for {service} remains unpaid. The grace period "
            f"ends at {grace}. Service suspension may follow."
        ),
        NotificationEvent.SERVICE_SUSPENDED: (
            f"{service} was suspended because invoice {invoice} remains unpaid. "
            "Pay the invoice in the reseller panel to request automatic recovery."
        ),
        NotificationEvent.PAYMENT_RECOVERED: (
            f"Payment for invoice {invoice} was received. Services suspended by "
            "recurring billing have been restored where safe."
        ),
        NotificationEvent.TOPUP_RECEIVED: (
            f"Your reseller account top-up of {amount} was received for invoice "
            f"{invoice}."
        ),
        NotificationEvent.USDT_PENDING: (
            f"A USDT transfer for invoice {invoice} was detected and is awaiting "
            "the required blockchain confirmations."
        ),
        NotificationEvent.USDT_DETECTED: (
            f"A USDT transfer for invoice {invoice} was detected after the "
            "required confirmations. Crediting is being processed."
        ),
    }
    return subject, bodies.get(event, "There is an update to your Rackflow account.")


class NotificationService:
    @staticmethod
    def enqueue(
        db: Session,
        *,
        reseller: Reseller,
        event: str,
        idempotency_key: str,
        data: Optional[dict] = None,
        recipient: Optional[str] = None,
    ) -> NotificationOutbox:
        email = (recipient or (reseller.user.email if reseller.user else "")).strip()
        if not email or "@" not in email:
            raise ValueError("Notification recipient is unavailable")
        existing = db.execute(
            select(NotificationOutbox).where(
                NotificationOutbox.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = NotificationOutbox(
            idempotency_key=idempotency_key[:255],
            reseller_id=reseller.id,
            recipient=email[:320],
            event=event[:64],
            template=event[:64],
            data=dict(data or {}),
            status=NotificationOutboxStatus.QUEUED,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            return db.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.idempotency_key == idempotency_key[:255]
                )
            ).scalar_one()
        return row


class NotificationTransport(Protocol):
    def send(self, row: NotificationOutbox) -> None: ...


class SMTPNotificationTransport:
    def __init__(self, config: Settings = settings) -> None:
        self.config = config

    def send(self, row: NotificationOutbox) -> None:
        subject, body = render_notification(row.template, row.data or {})
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = str(self.config.smtp_from)
        message["To"] = row.recipient
        message.set_content(body)
        with smtplib.SMTP(
            str(self.config.smtp_host),
            self.config.smtp_port,
            timeout=self.config.smtp_timeout_seconds,
        ) as client:
            client.ehlo()
            if self.config.smtp_starttls:
                tls_context = ssl.create_default_context(
                    purpose=ssl.Purpose.SERVER_AUTH
                )
                tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
                tls_context.check_hostname = True
                tls_context.verify_mode = ssl.CERT_REQUIRED
                client.starttls(context=tls_context)
                client.ehlo()
            if self.config.smtp_username and self.config.smtp_password:
                client.login(
                    self.config.smtp_username,
                    self.config.smtp_password.get_secret_value(),
                )
            client.send_message(message)


class NotificationOutboxSender:
    def __init__(
        self,
        *,
        config: Settings = settings,
        transport: Optional[NotificationTransport] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.transport = transport or SMTPNotificationTransport(config)
        self.clock = clock

    @staticmethod
    def _locked(statement, db: Session):
        if db.get_bind().dialect.name in {"mysql", "postgresql"}:
            return statement.with_for_update(skip_locked=True)
        return statement.with_for_update()

    def _claim(
        self,
        db: Session,
        *,
        owner: str,
        batch_size: Optional[int] = None,
    ) -> list[int]:
        now = self.clock()
        due = or_(
            NotificationOutbox.next_attempt_at.is_(None),
            NotificationOutbox.next_attempt_at <= now,
        )
        available = or_(
            and_(
                NotificationOutbox.status.in_(
                    [
                        NotificationOutboxStatus.QUEUED,
                        NotificationOutboxStatus.FAILED,
                    ]
                ),
                due,
            ),
            and_(
                NotificationOutbox.status == NotificationOutboxStatus.SENDING,
                NotificationOutbox.claim_expires_at <= now,
            ),
        )
        statement = (
            select(NotificationOutbox)
            .where(available)
            .order_by(NotificationOutbox.id)
            .limit(batch_size or self.config.notification_worker_batch_size)
        )
        rows = list(db.execute(self._locked(statement, db)).scalars())
        expires = now + timedelta(
            seconds=self.config.notification_worker_lease_seconds
        )
        for row in rows:
            row.status = NotificationOutboxStatus.SENDING
            row.claim_token = owner
            row.claim_expires_at = expires
        db.commit()
        return [row.id for row in rows]

    def send_batch(
        self,
        db: Session,
        *,
        owner: str,
        batch_size: Optional[int] = None,
    ) -> int:
        row_ids = self._claim(db, owner=owner, batch_size=batch_size)
        sent = 0
        for row_id in row_ids:
            row = db.get(NotificationOutbox, row_id)
            if row is None or row.claim_token != owner:
                continue
            if not self.config.smtp_enabled:
                row.status = NotificationOutboxStatus.SKIPPED
                row.error = "SMTP delivery is disabled"
                row.claim_token = None
                row.claim_expires_at = None
                db.commit()
                continue
            try:
                self.transport.send(row)
            except Exception as exc:
                row.attempts += 1
                row.status = NotificationOutboxStatus.FAILED
                row.error = f"SMTP delivery failed ({type(exc).__name__})"
                delay = min(3600, 60 * (2 ** min(row.attempts - 1, 6)))
                row.next_attempt_at = self.clock() + timedelta(seconds=delay)
            else:
                row.attempts += 1
                row.status = NotificationOutboxStatus.SENT
                row.sent_at = self.clock()
                row.error = None
                row.next_attempt_at = None
                sent += 1
            row.claim_token = None
            row.claim_expires_at = None
            db.commit()
        return sent
