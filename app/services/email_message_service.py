"""Durable email enqueueing for commerce and account lifecycle events."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.commerce_email import EmailMessage, EmailMessageStatus
from app.models.reseller import Reseller
from app.services.notification_service import NotificationEvent, render_notification


class EmailEvent:
    WELCOME = "welcome"
    VERIFY_EMAIL = "verify_email"
    PASSWORD_RESET = "password_reset"
    ORDER_CONFIRMATION = "order_confirmation"
    INVOICE_CREATED = "invoice_created"
    INVOICE_PAID = "invoice_paid"
    INVOICE_OVERDUE = "invoice_overdue"
    PAYMENT_FAILED = "payment_failed"
    SERVICE_PROVISIONED = "service_provisioned"
    SERVICE_SUSPENDED = "service_suspended"
    TICKET_REPLY = "ticket_reply"


_SUBJECTS = {
    EmailEvent.WELCOME: "Welcome to Rackflow",
    EmailEvent.VERIFY_EMAIL: "Verify your Rackflow email address",
    EmailEvent.PASSWORD_RESET: "Reset your Rackflow password",
    EmailEvent.ORDER_CONFIRMATION: "Your Rackflow order confirmation",
    EmailEvent.INVOICE_CREATED: "New Rackflow invoice",
    EmailEvent.INVOICE_PAID: "Rackflow invoice paid",
    EmailEvent.INVOICE_OVERDUE: "Rackflow invoice overdue",
    EmailEvent.PAYMENT_FAILED: "Rackflow payment failed",
    EmailEvent.SERVICE_PROVISIONED: "Your Rackflow service is ready",
    EmailEvent.SERVICE_SUSPENDED: "Your Rackflow service was suspended",
    EmailEvent.TICKET_REPLY: "New reply to your support ticket",
}


def _money(data: dict) -> str:
    cents = int(data.get("amount_cents") or 0)
    currency = str(data.get("currency") or "USD").upper()
    return f"{currency} {cents / 100:.2f}"


def render_email(event: str, data: dict) -> tuple[str, str, Optional[str]]:
    """Render subject, plain text, and optional HTML for an email event."""
    if event in {
        NotificationEvent.CYCLE_CHARGE_FAILED,
        NotificationEvent.PAYMENT_ACTION_REQUIRED,
        NotificationEvent.GRACE_WARNING,
        NotificationEvent.SERVICE_SUSPENDED,
        NotificationEvent.PAYMENT_RECOVERED,
        NotificationEvent.TOPUP_RECEIVED,
        NotificationEvent.USDT_PENDING,
        NotificationEvent.USDT_DETECTED,
    }:
        subject, body = render_notification(event, data)
        return subject, body, None

    subject = _SUBJECTS.get(event, "Rackflow notification")
    order = str(data.get("order_number") or data.get("order_id") or "")
    invoice = str(data.get("invoice_number") or data.get("invoice_id") or "")
    service = str(data.get("service_name") or "your service")
    ticket = str(data.get("ticket_number") or data.get("ticket_id") or "")
    amount = _money(data)
    verify_url = str(data.get("verify_url") or "")
    reset_url = str(data.get("reset_url") or "")

    bodies = {
        EmailEvent.WELCOME: (
            f"Welcome to Rackflow, {data.get('username', 'there')}! "
            "Your account is ready."
        ),
        EmailEvent.VERIFY_EMAIL: (
            "Please verify your email address to activate your account."
            + (f" Open this link: {verify_url}" if verify_url else "")
        ),
        EmailEvent.PASSWORD_RESET: (
            "We received a request to reset your password."
            + (f" Use this link: {reset_url}" if reset_url else "")
        ),
        EmailEvent.ORDER_CONFIRMATION: (
            f"Thank you for your order {order}. We received your request and "
            "will notify you when provisioning completes."
        ),
        EmailEvent.INVOICE_CREATED: (
            f"Invoice {invoice} for {amount} is now available in your account."
        ),
        EmailEvent.INVOICE_PAID: (
            f"Payment of {amount} for invoice {invoice} was received. Thank you."
        ),
        EmailEvent.INVOICE_OVERDUE: (
            f"Invoice {invoice} for {amount} is overdue. Please sign in to pay."
        ),
        EmailEvent.PAYMENT_FAILED: (
            f"A payment attempt for invoice {invoice} ({amount}) failed. "
            "Please update your payment method or try again."
        ),
        EmailEvent.SERVICE_PROVISIONED: (
            f"{service} has been provisioned and is ready to use."
        ),
        EmailEvent.SERVICE_SUSPENDED: (
            f"{service} was suspended. Sign in to review billing details."
        ),
        EmailEvent.TICKET_REPLY: (
            f"There is a new reply on support ticket {ticket}."
        ),
    }
    body = bodies.get(event, "There is an update to your Rackflow account.")
    html = f"<p>{body}</p>" if body else None
    return subject, body, html


class EmailMessageService:
    @staticmethod
    def enqueue(
        db: Session,
        *,
        to_address: str,
        event: str,
        idempotency_key: str,
        data: Optional[dict[str, Any]] = None,
        billing_account_id: Optional[int] = None,
        user_id: Optional[int] = None,
        related_type: Optional[str] = None,
        related_id: Optional[str] = None,
    ) -> EmailMessage:
        email = to_address.strip()
        if not email or "@" not in email:
            raise ValueError("Email recipient is unavailable")

        existing = db.execute(
            select(EmailMessage).where(
                EmailMessage.idempotency_key == idempotency_key[:255]
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        subject, body_text, body_html = render_email(event, dict(data or {}))
        row = EmailMessage(
            billing_account_id=billing_account_id,
            user_id=user_id,
            to_address=email[:320],
            subject=subject[:998],
            body_text=body_text,
            body_html=body_html,
            template_key=event[:128],
            event=event[:64],
            status=EmailMessageStatus.QUEUED,
            related_type=related_type[:64] if related_type else None,
            related_id=str(related_id)[:255] if related_id is not None else None,
            idempotency_key=idempotency_key[:255],
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            return db.execute(
                select(EmailMessage).where(
                    EmailMessage.idempotency_key == idempotency_key[:255]
                )
            ).scalar_one()
        return row

    @staticmethod
    def enqueue_for_reseller(
        db: Session,
        *,
        reseller: Reseller,
        event: str,
        idempotency_key: str,
        data: Optional[dict[str, Any]] = None,
        recipient: Optional[str] = None,
    ) -> EmailMessage:
        email = (recipient or (reseller.user.email if reseller.user else "")).strip()
        return EmailMessageService.enqueue(
            db,
            to_address=email,
            event=event,
            idempotency_key=idempotency_key,
            data=data,
            user_id=reseller.user_id,
        )

    @staticmethod
    def list_for_user(
        db: Session,
        user_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EmailMessage]:
        return list(
            db.execute(
                select(EmailMessage)
                .where(EmailMessage.user_id == user_id)
                .order_by(EmailMessage.created_at.desc(), EmailMessage.id.desc())
                .offset(offset)
                .limit(limit)
            ).scalars()
        )

    @staticmethod
    def list_admin(
        db: Session,
        *,
        status: Optional[EmailMessageStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EmailMessage]:
        stmt = select(EmailMessage).order_by(
            EmailMessage.created_at.desc(), EmailMessage.id.desc()
        )
        if status is not None:
            stmt = stmt.where(EmailMessage.status == status)
        return list(db.execute(stmt.offset(offset).limit(limit)).scalars())

    @staticmethod
    def retry(db: Session, message_id: int) -> Optional[EmailMessage]:
        row = db.get(EmailMessage, message_id)
        if row is None:
            return None
        if row.status not in {EmailMessageStatus.FAILED, EmailMessageStatus.SKIPPED}:
            return row
        row.status = EmailMessageStatus.QUEUED
        row.error = None
        db.flush()
        return row
