"""Senders of the email events added in billing phase 2 (docs/billing-phase2-design.md 12.2).

One small function per sender, called by other services. Each passes only the
variables the event's catalogue entry documents, queues the message through
``EmailMessageService.enqueue(variables=...)`` (so the event's template
applies) and never raises: a mail problem must not undo the payment, the
fulfilment or the reply that caused it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.commerce_email import EmailMessage
from app.models.commerce_order import Order, OrderItem
from app.models.reseller import Reseller
from app.models.storefront import FrontendProduct
from app.services.email_message_service import EmailEvent, EmailMessageService

logger = logging.getLogger(__name__)


def _send(db: Session, label: str, build: Callable[[], Optional[EmailMessage]]) -> Optional[EmailMessage]:
    """Run ``build`` in a savepoint; a failure is logged and leaves the caller's transaction intact."""
    try:
        with db.begin_nested():
            return build()
    except Exception:  # noqa: BLE001 - mail must not break the flow that triggers it
        logger.warning("Could not queue the %s email", label, exc_info=True)
        return None


def _first_order_product_id(db: Session, user_id: int) -> Optional[int]:
    """The catalogue product of the first item of the user's first order, if any."""
    return db.execute(
        select(FrontendProduct.product_id)
        .join(OrderItem, OrderItem.frontend_product_id == FrontendProduct.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.user_id == user_id)
        .order_by(Order.id, OrderItem.id)
        .limit(1)
    ).scalar_one_or_none()


def send_service_provisioned(db: Session, service: Any) -> Optional[EmailMessage]:
    """Tell the owner that a service is ready (key ``service:{id}:provisioned``)."""

    def build() -> Optional[EmailMessage]:
        owner = getattr(service, "owner_user", None)
        if owner is None or not owner.email:
            return None
        return EmailMessageService.enqueue(
            db,
            to_address=owner.email,
            event=EmailEvent.SERVICE_PROVISIONED,
            idempotency_key=f"service:{service.id}:provisioned",
            variables={"service_name": service.name},
            user_id=owner.id,
            related_type="service",
            related_id=str(service.id),
        )

    return _send(db, "service provisioned", build)


def send_welcome(db: Session, user: Any) -> Optional[EmailMessage]:
    """Welcome a user after email verification (key ``user:{id}:welcome``).

    The variant for the product of the user's first order applies when there
    is one.
    """

    def build() -> Optional[EmailMessage]:
        if not user.email:
            return None
        return EmailMessageService.enqueue(
            db,
            to_address=user.email,
            event=EmailEvent.WELCOME,
            idempotency_key=f"user:{user.id}:welcome",
            variables={"username": user.username},
            user_id=user.id,
            product_id=_first_order_product_id(db, user.id),
            related_type="user",
            related_id=str(user.id),
        )

    return _send(db, "welcome", build)


def send_payment_failed(db: Session, payment: Any) -> Optional[EmailMessage]:
    """Tell the payer that a card payment attempt failed (key ``payment:{id}:failed``)."""

    def build() -> Optional[EmailMessage]:
        invoice = payment.invoice
        account = getattr(invoice, "billing_account", None)
        user = account.user if account is not None else None
        if user is None and getattr(invoice, "reseller_id", None) is not None:
            reseller = db.get(Reseller, invoice.reseller_id)
            user = reseller.user if reseller is not None else None
        if user is None or not user.email:
            return None
        return EmailMessageService.enqueue(
            db,
            to_address=user.email,
            event=EmailEvent.PAYMENT_FAILED,
            idempotency_key=f"payment:{payment.id}:failed",
            variables={
                "invoice_number": invoice.invoice_number,
                "invoice_id": invoice.id,
                "amount_cents": int(payment.amount_cents),
                "currency": payment.currency,
            },
            billing_account_id=invoice.billing_account_id,
            user_id=user.id,
            related_type="payment",
            related_id=str(payment.id),
        )

    return _send(db, "payment failed", build)


def send_ticket_reply(db: Session, ticket: Any, message: Any) -> Optional[EmailMessage]:
    """Tell the ticket owner about a staff reply (key ``ticket_reply:{message id}``)."""

    def build() -> Optional[EmailMessage]:
        user = ticket.user
        if user is None or not user.email:
            return None
        return EmailMessageService.enqueue(
            db,
            to_address=user.email,
            event=EmailEvent.TICKET_REPLY,
            idempotency_key=f"ticket_reply:{message.id}",
            variables={"ticket_number": str(ticket.ticket_number), "ticket_id": ticket.id},
            billing_account_id=ticket.billing_account_id,
            user_id=user.id,
            related_type="ticket",
            related_id=str(ticket.id),
        )

    return _send(db, "ticket reply", build)
