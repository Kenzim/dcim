"""Order cancellation clean-up and the order refund policy.

Until credit notes and the refund engine exist (phase 1, A2 and A4), every
policy ends in a recorded manual review: no money moves automatically.
``PaymentRefundService`` is not used for ``original_method`` because it marks a
payment refunded when Stripe reports the refund as still pending, and it calls
the gateway before the local record is committed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.dao.order_dao import OrderDAO, OrderStatusHistoryDAO
from app.models.commerce_order import Order, OrderStatus
from app.models.reseller import Invoice, InvoiceStatus, Payment
from app.services.audit_service import AuditService
from app.services.billing.coupons import release_coupon_usage
from app.services.billing.settings_access import billing_setting

logger = logging.getLogger(__name__)

REFUND_POLICIES = ("manual_review", "original_method", "credit")

_POLICY_NOTES = {
    "manual_review": "held for manual review",
    "original_method": (
        "refund to the original payment method is not automated until the refund "
        "engine exists; held for manual review"
    ),
    "credit": (
        "retail accounts have no credit balance yet; held for manual review"
    ),
}


class OrderCancelError(ValueError):
    pass


@dataclass(frozen=True)
class RefundDecision:
    policy: str
    handling: str
    amount_cents: int
    currency: str


def _marker(reason: str, invoice: Invoice, payment: Optional[Payment]) -> str:
    if payment is not None:
        return f"[refund-review:{reason}:payment-{payment.id}]"
    return f"[refund-review:{reason}:invoice-{invoice.id}]"


def _invoice_review_reasons(notes: str, invoice: Invoice) -> list[str]:
    """Reasons of the invoice-level reviews already in the order notes, oldest first.

    Notes written before the reason was part of the marker carry
    ``[refund-review:invoice-{id}]`` and name the reason as ``({reason});``.
    """
    current = re.compile(rf"\[refund-review:(\w+):invoice-{invoice.id}\]")
    legacy_marker = f"[refund-review:invoice-{invoice.id}]"
    legacy_reason = re.compile(r"\((\w+)\);")
    reasons: list[str] = []
    for line in notes.splitlines():
        found = current.search(line)
        if found is None and legacy_marker in line:
            found = legacy_reason.search(line)
        if found is not None and found.group(1) not in reasons:
            reasons.append(found.group(1))
    return reasons


def _refund_policy(db: Session) -> str:
    value = str(billing_setting(db, "billing_order_refund_policy") or "manual_review")
    return value if value in REFUND_POLICIES else "manual_review"


def record_refund_review(
    db: Session,
    *,
    invoice: Invoice,
    amount_cents: int,
    reason: str,
    payment: Optional[Payment] = None,
    actor_user_id: Optional[int] = None,
) -> RefundDecision:
    """Record money that needs a human refund decision. Idempotent per reason and payment."""
    policy = _refund_policy(db)
    decision = RefundDecision(
        policy=policy,
        handling="manual_review",
        amount_cents=int(amount_cents),
        currency=invoice.currency,
    )
    marker = _marker(reason, invoice, payment)
    order = db.get(Order, invoice.order_id) if invoice.order_id is not None else None
    already_under_review: list[str] = []
    if payment is not None:
        if (payment.payment_metadata or {}).get("refund_review", {}).get(reason):
            return decision
        payment.payment_metadata = {
            **(payment.payment_metadata or {}),
            "refund_review": {
                **(payment.payment_metadata or {}).get("refund_review", {}),
                reason: {"amount_cents": int(amount_cents), "policy": policy},
            },
        }
    elif order is not None:
        earlier = _invoice_review_reasons(order.admin_notes or "", invoice)
        if reason in earlier:
            return decision
        already_under_review = earlier
    if already_under_review:
        # The same invoice money is already listed for review under another
        # reason; this reason is recorded without listing the money twice.
        message = (
            f"{marker} {reason}: the {int(amount_cents)} {invoice.currency} on invoice "
            f"{invoice.invoice_number} is already held for refund review "
            f"({', '.join(already_under_review)})."
        )
    else:
        message = (
            f"{marker} Refund review required: {int(amount_cents)} {invoice.currency} "
            f"on invoice {invoice.invoice_number} ({reason}); policy {policy}, "
            f"{_POLICY_NOTES[policy]}."
        )
    if order is not None:
        order.admin_notes = ((order.admin_notes or "") + "\n" + message).strip()
    AuditService.log(
        db,
        actor_user_id=actor_user_id,
        billing_account_id=invoice.billing_account_id,
        action="billing.refund_review",
        resource_type="invoice",
        resource_id=str(invoice.id),
        metadata={
            "reason": reason,
            "policy": policy,
            "handling": decision.handling,
            "amount_cents": int(amount_cents),
            "currency": invoice.currency,
            "order_id": invoice.order_id,
            "payment_id": payment.id if payment is not None else None,
            "already_under_review": already_under_review,
        },
    )
    logger.warning(message)
    db.flush()
    return decision


def apply_order_refund_policy(
    db: Session,
    order: Order,
    *,
    reason: str,
    actor_user_id: Optional[int] = None,
) -> Optional[RefundDecision]:
    """Apply the refund policy to money already received for an order."""
    from app.services.invoice_service import InvoiceService

    if order.invoice_id is None:
        return None
    invoice = db.get(Invoice, order.invoice_id)
    if invoice is None:
        return None
    paid = InvoiceService.allocated_cents(db, invoice.id)
    if paid <= 0:
        return None
    return record_refund_review(
        db,
        invoice=invoice,
        amount_cents=paid,
        reason=reason,
        actor_user_id=actor_user_id,
    )


def cancel_order(
    db: Session,
    order: Order,
    *,
    actor_user_id: Optional[int] = None,
    note: str = "Cancelled by admin",
) -> tuple[Order, Optional[RefundDecision]]:
    """Cancel an order, void its unpaid invoice, and apply the refund policy to money received."""
    from app.services.invoice_service import InvoiceService

    locked = OrderDAO.get_for_update(db, order.id)
    if locked is None:
        raise OrderCancelError("Order not found")
    if locked.status in {OrderStatus.ACTIVE, OrderStatus.CANCELLED}:
        raise OrderCancelError(
            f"Order cannot be cancelled from status {locked.status.value}"
        )
    decision: Optional[RefundDecision] = None
    if locked.invoice_id is not None:
        invoice = InvoiceService.get_for_update(db, locked.invoice_id)
        if invoice is not None and invoice.status not in {
            InvoiceStatus.PAID,
            InvoiceStatus.VOID,
        }:
            invoice.status = InvoiceStatus.VOID
        decision = apply_order_refund_policy(
            db, locked, reason="order_cancelled", actor_user_id=actor_user_id
        )
    if decision is None:
        # No money is held for the order, so its coupon use is given back.
        release_coupon_usage(
            db, order=locked, actor_user_id=actor_user_id, reason="order_cancelled"
        )
    prior = locked.status
    locked.status = OrderStatus.CANCELLED
    OrderStatusHistoryDAO.append(
        db,
        order_id=locked.id,
        from_status=prior,
        to_status=OrderStatus.CANCELLED,
        actor_user_id=actor_user_id,
        note=note,
    )
    db.flush()
    return locked, decision
