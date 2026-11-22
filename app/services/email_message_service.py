"""Durable email enqueueing for commerce and account lifecycle events."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.commerce_email import EmailMessage, EmailMessageStatus
from app.models.reseller import Reseller
from app.services.email_templates.catalogue import builtin_email
from app.services.email_templates.render import render as render_template


class EmailEvent:
    WELCOME = "welcome"
    VERIFY_EMAIL = "verify_email"
    PASSWORD_RESET = "password_reset"
    USER_INVITE = "user_invite"
    ORDER_CONFIRMATION = "order_confirmation"
    INVOICE_CREATED = "invoice_created"
    INVOICE_PAID = "invoice_paid"
    INVOICE_OVERDUE = "invoice_overdue"
    PAYMENT_FAILED = "payment_failed"
    SERVICE_PROVISIONED = "service_provisioned"
    SERVICE_SUSPENDED = "service_suspended"
    TICKET_REPLY = "ticket_reply"


def render_email(event: str, data: dict) -> tuple[str, str, Optional[str]]:
    """Render subject, plain text, and optional HTML for an email event from the built-in texts."""
    return builtin_email(event, data)


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
        variables: Optional[dict[str, Any]] = None,
        locale: str = "en",
        product_id: Optional[int] = None,
    ) -> EmailMessage:
        """Queue an email, rendered now and stored with its text.

        With ``variables`` the text comes from the event's email template
        (``app.services.email_templates.render``): a template that fails falls
        back to the built-in text (``render_fallback``), and a disabled
        template stores the message as ``skipped``. With ``data`` only, the
        built-in text is used.
        """
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

        status = EmailMessageStatus.QUEUED
        template_id = template_version = None
        render_fallback = False
        error = None
        if variables is not None:
            rendered = render_template(
                db, event, locale=locale, variables=variables, product_id=product_id
            )
            subject, body_text, body_html = rendered.subject, rendered.text, rendered.html
            template_id, template_version = rendered.template_id, rendered.template_version
            render_fallback = rendered.fallback
            if rendered.skipped:
                status = EmailMessageStatus.SKIPPED
                error = "Skipped: the email template for this event is disabled"
        else:
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
            status=status,
            error=error,
            template_id=template_id,
            template_version=template_version,
            render_fallback=render_fallback,
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
