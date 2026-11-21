"""Order cancellation clean-up, the order refund policy and refund reviews.

Money that may need returning is recorded as a refund review (order notes,
payment metadata, audit ``billing.refund_review``) for staff to resolve with
``resolve_refund_review``: refund to the original method, credit the account,
or dismiss. When the invoice engine is ``v2`` the policies ``original_method``
and ``credit`` refund a cancelled order's payments through ``RefundService``
instead; gateway refunds are reserved in the cancelling transaction and sent
afterwards (``RefundService.dispatch`` or the refund reconciler), because no
lock may be held across a gateway call. In ``legacy`` and ``shadow``, and for
provisioning failures, every policy stays a manual review.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dao.order_dao import OrderDAO, OrderStatusHistoryDAO
from app.models.commerce_order import Order, OrderStatus
from app.models.refund import RefundDestination, RefundReason, RefundStatus
from app.models.reseller import Invoice, InvoiceStatus, Payment
from app.services.audit_service import AuditService
from app.services.billing.coupons import release_coupon_usage
from app.services.billing.settings_access import billing_setting
from app.services.billing_locks import BillingLocks

logger = logging.getLogger(__name__)

REFUND_POLICIES = ("manual_review", "original_method", "credit")
REVIEW_ACTIONS = ("refund_original_method", "credit", "dismiss")
# Reasons whose money the v2 policies refund automatically (design 7.7).
AUTOMATIC_REASONS = ("order_cancelled",)

_POLICY_NOTES = {
    "manual_review": "held for manual review",
    "original_method": (
        "refunds to the original payment method are automatic only for cancelled orders "
        "with the v2 invoice engine; held for manual review"
    ),
    "credit": (
        "refunds to account credit are automatic only for cancelled orders with the v2 "
        "invoice engine; held for manual review"
    ),
}


class OrderCancelError(ValueError):
    pass


class RefundReviewError(ValueError):
    def __init__(self, message: str, *, code: str = "refund_review_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RefundDecision:
    policy: str
    handling: str
    amount_cents: int
    currency: str
    refund_ids: tuple[int, ...] = field(default=())


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


def _automatic_policy(db: Session, reason: str) -> Optional[str]:
    """The policy to apply without staff, or None for a manual review."""
    from app.services.refund_service import invoice_engine_mode

    policy = _refund_policy(db)
    if policy == "manual_review" or reason not in AUTOMATIC_REASONS:
        return None
    if invoice_engine_mode(db) != "v2":
        return None
    return policy


def record_refund_review(
    db: Session,
    *,
    invoice: Invoice,
    amount_cents: int,
    reason: str,
    payment: Optional[Payment] = None,
    actor_user_id: Optional[int] = None,
    detail: Optional[str] = None,
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
            f"{detail or _POLICY_NOTES[policy]}."
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
    policy = _automatic_policy(db, reason)
    if policy is not None:
        return _refund_order_payments(
            db, order, invoice, policy=policy, reason=reason, actor_user_id=actor_user_id
        )
    return record_refund_review(
        db,
        invoice=invoice,
        amount_cents=paid,
        reason=reason,
        actor_user_id=actor_user_id,
    )


def _refund_order_payments(
    db: Session,
    order: Order,
    invoice: Invoice,
    *,
    policy: str,
    reason: str,
    actor_user_id: Optional[int],
) -> RefundDecision:
    """Refund every refundable payment of the order's invoice by ``policy``.

    Gateway refunds are only reserved here (``defer_gateway``); a payment that
    cannot be refunded automatically is recorded for review instead.
    """
    from app.services.refund_service import (
        CREDIT_GATEWAY,
        GATEWAY_REFUNDABLE,
        REFUNDABLE_PAYMENT_STATUSES,
        USDT_GATEWAYS,
        RefundError,
        RefundService,
        refundable_cents,
    )

    payments = list(
        db.execute(
            select(Payment)
            .where(
                Payment.invoice_id == invoice.id,
                Payment.status.in_(REFUNDABLE_PAYMENT_STATUSES),
            )
            .order_by(Payment.id)
        ).scalars()
    )
    refund_ids: list[int] = []
    refunded = 0
    pending = False
    reviewed = False
    for payment in payments:
        amount = refundable_cents(db, payment)
        if amount <= 0:
            continue
        gateway = (payment.gateway or "").lower()
        if policy == "credit" or gateway == CREDIT_GATEWAY or gateway in USDT_GATEWAYS:
            destination = RefundDestination.CREDIT
        elif gateway in GATEWAY_REFUNDABLE and (payment.external_ref or "").strip():
            destination = RefundDestination.GATEWAY
        else:
            record_refund_review(
                db, invoice=invoice, amount_cents=amount, reason=reason,
                payment=payment, actor_user_id=actor_user_id,
            )
            reviewed = True
            continue
        try:
            with db.begin_nested():
                refund = RefundService.request(
                    db,
                    payment_id=payment.id,
                    amount_cents=amount,
                    destination=destination,
                    reason=RefundReason.CANCELLATION,
                    actor_user_id=actor_user_id,
                    idempotency_key=f"order-cancel:{order.id}:{payment.id}",
                    reason_text=f"Order {order.order_number} cancelled",
                    defer_gateway=True,
                )
        except RefundError as exc:
            logger.warning("Order %s: automatic refund of payment %s refused: %s", order.id, payment.id, exc)
            record_refund_review(
                db, invoice=invoice, amount_cents=amount, reason=reason,
                payment=payment, actor_user_id=actor_user_id,
                detail=f"the automatic refund was refused ({exc.code}): {exc}; held for manual review",
            )
            reviewed = True
            continue
        refund_ids.append(int(refund.id))
        refunded += int(refund.amount_cents)
        pending = pending or refund.status == RefundStatus.PENDING
    handling = "refund_pending" if pending else "refunded"
    if reviewed:
        handling = "manual_review" if not refund_ids else f"{handling}_partly_reviewed"
    message = (
        f"[order-refund:{reason}:invoice-{invoice.id}] {refunded} {invoice.currency} on invoice "
        f"{invoice.invoice_number} refunded by policy {policy} "
        f"(refunds {', '.join(str(i) for i in refund_ids) or 'none'}; {handling})."
    )
    order.admin_notes = ((order.admin_notes or "") + "\n" + message).strip()
    AuditService.log(
        db,
        actor_user_id=actor_user_id,
        billing_account_id=invoice.billing_account_id,
        action="billing.order_refund",
        resource_type="invoice",
        resource_id=str(invoice.id),
        metadata={
            "reason": reason,
            "policy": policy,
            "handling": handling,
            "amount_cents": refunded,
            "currency": invoice.currency,
            "order_id": order.id,
            "refund_ids": refund_ids,
        },
    )
    db.flush()
    return RefundDecision(
        policy=policy,
        handling=handling,
        amount_cents=refunded,
        currency=invoice.currency,
        refund_ids=tuple(refund_ids),
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
    from app.services.refund_service import set_invoice_status

    if order.invoice_id is not None and _automatic_policy(db, "order_cancelled") is not None:
        # Refunds write the account's ledger, whose lock comes before the order's.
        BillingLocks.for_invoice(db, int(order.invoice_id), reseller=True, billing_account=True)
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
            set_invoice_status(db, invoice, InvoiceStatus.VOID, cause="order_cancelled", actor_user_id=actor_user_id)
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


def _resolution_marker(reason: str, invoice: Invoice) -> str:
    return f"[refund-review-resolved:{reason}:invoice-{invoice.id}]"


def _review_state(
    db: Session, *, invoice: Invoice, payment: Optional[Payment], reason: str
) -> tuple[Optional[dict], Optional[dict]]:
    """The open review and its resolution (each None when absent) for a reason."""
    if payment is not None:
        metadata = payment.payment_metadata or {}
        review = (metadata.get("refund_review") or {}).get(reason)
        resolution = (metadata.get("refund_review_resolved") or {}).get(reason)
        if review is not None:
            return dict(review), resolution
    order = db.get(Order, invoice.order_id) if invoice.order_id is not None else None
    notes = (order.admin_notes or "") if order is not None else ""
    if reason in _invoice_review_reasons(notes, invoice):
        resolved = _resolution_marker(reason, invoice) in notes
        return {"amount_cents": None, "invoice_level": True}, ({"recorded": True} if resolved else None)
    return None, None


def resolve_refund_review(
    db: Session,
    *,
    invoice_id: int,
    reason: str,
    action: str,
    actor_user_id: Optional[int],
    idempotency_key: str,
    payment_id: Optional[int] = None,
    amount_cents: Optional[int] = None,
    note: Optional[str] = None,
    registry: Any = None,
) -> dict:
    """Resolve a refund review: refund to the original method, credit the account, or dismiss.

    A refund goes through ``RefundService.request`` (a gateway refund commits
    its reservation and calls the gateway with no lock held). The resolution
    is recorded on the payment metadata or the order notes and audited as
    ``billing.refund_review_resolved``; a refund the gateway declined leaves
    the review open. The caller commits.
    """
    from app.services.refund_service import (
        CREDIT_GATEWAY,
        RefundError,
        RefundService,
        refundable_cents,
    )

    if action not in REVIEW_ACTIONS:
        raise RefundReviewError(f"Unknown action {action!r}", code="invalid_action")
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise RefundReviewError("Invoice not found", code="not_found")
    if payment_id is not None:
        payment = BillingLocks.for_payment(db, int(payment_id), reseller=True, billing_account=True)
        if payment is None or payment.invoice_id != invoice.id:
            raise RefundReviewError("Payment not found on this invoice", code="not_found")
    else:
        payment = None
        BillingLocks.for_invoice(db, invoice.id, reseller=True, billing_account=True)
    review, resolution = _review_state(db, invoice=invoice, payment=payment, reason=reason)
    if review is None:
        raise RefundReviewError("No refund review with this reason", code="not_found")
    if resolution is not None:
        if resolution.get("idempotency_key") == idempotency_key or resolution.get("recorded"):
            return {"status": "already_resolved", "resolution": resolution}
        raise RefundReviewError("This refund review is already resolved", code="already_resolved")
    if action == "dismiss" and not (note or "").strip():
        raise RefundReviewError("Dismissing a refund review needs a note", code="note_required")
    if action != "dismiss" and payment is None:
        raise RefundReviewError("A refund needs the payment to refund", code="payment_required")

    record = {
        "action": action,
        "refund_id": None,
        "refund_status": None,
        "actor_user_id": actor_user_id,
        "note": (note or "").strip() or None,
        "idempotency_key": idempotency_key,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    payment_level = payment is not None and not review.get("invoice_level")

    def _store(value: Optional[dict]) -> None:
        metadata = dict(payment.payment_metadata or {})
        resolved = dict(metadata.get("refund_review_resolved") or {})
        if value is None:
            resolved.pop(reason, None)
        else:
            resolved[reason] = value
        metadata["refund_review_resolved"] = resolved
        payment.payment_metadata = metadata

    refund = None
    if action != "dismiss":
        amount = amount_cents
        if amount is None:
            amount = min(int(review.get("amount_cents") or 0) or refundable_cents(db, payment), refundable_cents(db, payment))
        if action == "credit" or (payment.gateway or "").lower() == CREDIT_GATEWAY:
            destination = RefundDestination.CREDIT
        else:
            destination = RefundDestination.GATEWAY
        if payment_level:
            # Claimed before the refund: it commits with the refund's reservation.
            _store(record)
        payment_id_value = payment.id
        try:
            refund = RefundService.request(
                db,
                payment_id=payment_id_value,
                amount_cents=int(amount),
                destination=destination,
                reason=RefundReason.CANCELLATION if reason == "order_cancelled" else RefundReason.OTHER,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                reason_text=(note or f"Refund review {reason}")[:512],
                registry=registry,
            )
        except RefundError as exc:
            raise RefundReviewError(str(exc), code=exc.code) from exc
        payment = BillingLocks.for_payment(db, payment_id_value, reseller=True, billing_account=True)
        if refund.status == RefundStatus.FAILED:
            if payment_level:
                _store(None)
            db.flush()
            return {"status": "refund_failed", "refund_id": refund.id, "review_open": True}
        record = {**record, "refund_id": refund.id, "refund_status": refund.status.value}
    if payment_level:
        _store(record)
    order = db.get(Order, invoice.order_id) if invoice.order_id is not None else None
    if order is not None:
        if payment is None or review.get("invoice_level"):
            marker = _resolution_marker(reason, invoice)
        else:
            marker = f"[refund-review-resolved:{reason}:payment-{payment.id}]"
        line = (
            f"{marker} {action}"
            + (f", refund {refund.id} ({refund.status.value})" if refund is not None else "")
            + (f": {record['note']}" if record["note"] else "")
        )
        order.admin_notes = ((order.admin_notes or "") + "\n" + line).strip()
    AuditService.log(
        db,
        actor_user_id=actor_user_id,
        billing_account_id=invoice.billing_account_id,
        action="billing.refund_review_resolved",
        resource_type="invoice",
        resource_id=str(invoice.id),
        metadata={
            "reason": reason,
            "payment_id": payment.id if payment is not None else None,
            "order_id": invoice.order_id,
            **record,
        },
    )
    db.flush()
    return {"status": "resolved", "resolution": record}


def list_refund_reviews(db: Session, *, limit: int = 100) -> list[dict]:
    """Refund reviews recorded by ``record_refund_review`` with their resolution state, newest first."""
    from app.models.commerce_audit import UserAuditEvent

    events = db.execute(
        select(UserAuditEvent)
        .where(UserAuditEvent.action == "billing.refund_review")
        .order_by(UserAuditEvent.id.desc())
        .limit(limit)
    ).scalars()
    items: list[dict] = []
    seen: set[tuple] = set()
    for event in events:
        meta = event.event_metadata or {}
        key = (event.resource_id, meta.get("payment_id"), meta.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        invoice = db.get(Invoice, int(event.resource_id))
        if invoice is None:
            continue
        payment = db.get(Payment, int(meta["payment_id"])) if meta.get("payment_id") else None
        _review, resolution = _review_state(db, invoice=invoice, payment=payment, reason=str(meta.get("reason")))
        items.append(
            {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "order_id": invoice.order_id,
                "payment_id": payment.id if payment is not None else None,
                "reason": meta.get("reason"),
                "policy": meta.get("policy"),
                "amount_cents": meta.get("amount_cents"),
                "currency": meta.get("currency"),
                "recorded_at": event.created_at,
                "resolved": resolution is not None,
                "resolution": resolution,
            }
        )
    return items
