"""Refunds of payments (docs/billing-phase1-design.md 7).

``RefundService`` is the only writer of ``refunds`` rows, of
``payments.refunded_cents`` and of the payment refund statuses. A refund is
never marked succeeded before the gateway confirms it, and no database lock is
held while a gateway is called: a gateway refund is reserved (a ``pending``
row, committed), sent with the row's idempotency key, and completed in a new
transaction. A gateway that does not answer leaves the row ``pending`` for the
reconciler (``app/services/payments/refund_reconciler.py``).

While the invoice engine is ``legacy`` or ``shadow`` only full refunds are
accepted and the side effects are the ones the reseller path has always had:
payment ``refunded``, invoice ``failed``, the top-up or credit ledger entry
reversed, a reseller gateway refund or dispute debited from the balance.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.commerce_account import BillingAccount
from app.models.refund import Refund, RefundDestination, RefundReason, RefundStatus
from app.models.reseller import (
    CreditLedgerEntry,
    CreditLedgerEntryType,
    Invoice,
    InvoiceDocumentType,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.services.audit_service import AuditService
from app.services.billing_locks import BillingLocks
from app.services.payments.base import (
    GatewayNotConfiguredError,
    GatewayRefundResult,
    GatewayResultStatus,
    PaymentGatewayError,
)

logger = logging.getLogger(__name__)

GATEWAY_REFUNDABLE = frozenset({"stripe", "paypal"})
CREDIT_GATEWAY = "credit"
USDT_GATEWAYS = frozenset({"usdt", "usdt_crypto"})
OPEN_STATUSES = (RefundStatus.PENDING, RefundStatus.REVIEW)
REFUNDABLE_PAYMENT_STATUSES = (PaymentStatus.SUCCEEDED, PaymentStatus.PARTIALLY_REFUNDED)

CLAIM_TTL = timedelta(minutes=10)
RETRY_DELAY = timedelta(minutes=5)
# Stripe keeps idempotency keys for 24 hours; the call is not re-sent after that.
GATEWAY_RETRY_WINDOW = timedelta(hours=23)
REVIEW_AFTER = timedelta(hours=24)
MAX_ATTEMPTS = 6


class RefundError(ValueError):
    def __init__(self, message: str, *, code: str = "refund_error") -> None:
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes; every stored time is UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def invoice_engine_mode(db: Session) -> str:
    from app.services.billing_shadow import billing_engine_mode

    return billing_engine_mode(db, "invoice")


def _registry(registry: Any) -> Any:
    if registry is not None:
        return registry
    from app.services.payments.registry import payment_gateway_registry

    return payment_gateway_registry


def set_invoice_status(
    db: Session, invoice: Invoice, to: InvoiceStatus, *, cause: str, actor_user_id: Optional[int] = None
) -> None:
    """Move an invoice through ``InvoiceService.transition``.

    While ``transition`` is still the phase 1 stub the status is written
    directly, as the refund paths did before phase 1.
    """
    from app.services.invoice_service import InvoiceService

    try:
        InvoiceService.transition(db, invoice, to, cause=cause, actor_user_id=actor_user_id)
    except NotImplementedError:
        invoice.status = to
    if to != InvoiceStatus.PAID:
        invoice.paid_at = None


def account_id_for(db: Session, invoice: Invoice) -> Optional[int]:
    if invoice.billing_account_id is not None:
        return int(invoice.billing_account_id)
    if invoice.reseller_id is None:
        return None
    found = db.scalar(
        select(BillingAccount.id).where(BillingAccount.reseller_id == invoice.reseller_id)
    )
    return int(found) if found is not None else None


@dataclass(frozen=True)
class _Locked:
    payment: Payment
    invoice: Invoice


def _lock_payment(db: Session, payment_id: int) -> _Locked:
    payment = BillingLocks.for_payment(db, payment_id, reseller=True, billing_account=True)
    if payment is None:
        raise RefundError("Payment not found", code="not_found")
    invoice = db.get(Invoice, payment.invoice_id)
    return _Locked(payment=payment, invoice=invoice)


def _lock_refund(db: Session, refund_id: int) -> tuple[_Locked, Refund]:
    payment_id = db.scalar(select(Refund.payment_id).where(Refund.id == refund_id))
    if payment_id is None:
        raise RefundError("Refund not found", code="not_found")
    locked = _lock_payment(db, int(payment_id))
    refund = BillingLocks.refund(db, refund_id)
    if refund is None:
        raise RefundError("Refund not found", code="not_found")
    return locked, refund


def open_refund_cents(db: Session, payment_id: int, *, exclude_id: Optional[int] = None) -> int:
    statement = select(func.coalesce(func.sum(Refund.amount_cents), 0)).where(
        Refund.payment_id == payment_id, Refund.status.in_(OPEN_STATUSES)
    )
    if exclude_id is not None:
        statement = statement.where(Refund.id != exclude_id)
    return int(db.scalar(statement) or 0)


def refundable_cents(db: Session, payment: Payment) -> int:
    """``amount - refunded - pending and review refunds`` (design 7.1)."""
    return max(
        0,
        int(payment.amount_cents)
        - int(payment.refunded_cents or 0)
        - open_refund_cents(db, payment.id),
    )


def _gateway_name(payment: Payment) -> str:
    return (payment.gateway or "").lower()


def _is_topup(invoice: Invoice) -> bool:
    return invoice.purpose == InvoicePurpose.CREDIT_TOPUP


def _default_credit_note(invoice: Invoice, reason: RefundReason) -> bool:
    if reason == RefundReason.DISPUTE or _is_topup(invoice):
        return False
    return invoice.document_type != InvoiceDocumentType.PROFORMA


class RefundService:
    # ----------------------------------------------------------------- request

    @staticmethod
    def request(
        db: Session,
        *,
        payment_id: int,
        amount_cents: int,
        destination: RefundDestination,
        reason: RefundReason,
        actor_user_id: Optional[int],
        idempotency_key: str,
        reason_text: Optional[str] = None,
        issue_credit_note: Optional[bool] = None,
        credit_note_lines: Optional[Sequence[Any]] = None,
        registry: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
        defer_gateway: bool = False,
    ) -> Refund:
        """Refund (part of) a payment; returns the refund row.

        Destination ``credit`` and ``manual`` complete in the caller's
        transaction (nothing is committed). Destination ``gateway`` commits the
        reserved ``pending`` row, calls the gateway with no lock held and
        records the answer in a new transaction that the caller commits; the
        refund comes back ``succeeded``, ``failed`` or still ``pending``. With
        ``defer_gateway`` the row is only reserved, in the caller's
        transaction, for ``dispatch`` (or the reconciler) to send later.
        """
        destination = RefundDestination(destination)
        reason = RefundReason(reason)
        key = (idempotency_key or "").strip()
        if not key:
            raise RefundError("An idempotency key is required", code="idempotency_key_required")
        if type(amount_cents) is not int or amount_cents <= 0:
            raise RefundError("Refund amount must be a positive whole number of cents", code="invalid_amount")
        params = {
            "payment_id": int(payment_id),
            "amount_cents": int(amount_cents),
            "destination": destination,
            "reason": reason,
        }
        existing = RefundService._by_key(db, key)
        if existing is not None:
            return RefundService._replay(existing, params)

        locked = _lock_payment(db, payment_id)
        existing = RefundService._by_key(db, key)
        if existing is not None:
            return RefundService._replay(existing, params)
        payment, invoice = locked.payment, locked.invoice
        mode = invoice_engine_mode(db)
        RefundService._validate(db, payment, invoice, amount_cents, destination, mode)
        adapter = None
        if destination == RefundDestination.GATEWAY:
            try:
                adapter = _registry(registry).get(_gateway_name(payment))
            except GatewayNotConfiguredError as exc:
                raise RefundError(str(exc), code="gateway_unavailable") from exc

        if mode != "v2":
            issue_credit_note = False
        elif issue_credit_note is None:
            issue_credit_note = _default_credit_note(invoice, reason)
        elif issue_credit_note and not _default_credit_note(invoice, reason):
            raise RefundError(
                "Refunds of top-ups, proformas and disputes do not issue credit notes",
                code="credit_note_not_allowed",
            )
        now = _now()
        refund = Refund(
            payment_id=payment.id,
            invoice_id=invoice.id,
            billing_account_id=account_id_for(db, invoice),
            reseller_id=invoice.reseller_id,
            amount_cents=int(amount_cents),
            currency=payment.currency,
            destination=destination,
            gateway=_gateway_name(payment),
            reason=reason,
            reason_text=(reason_text or "").strip()[:512] or None,
            issue_credit_note=bool(issue_credit_note),
            status=RefundStatus.PENDING,
            idempotency_key=key,
            attempts=0,
            requested_by_user_id=actor_user_id,
            requested_at=now,
            refund_metadata={
                **dict(metadata or {}),
                "engine": mode,
                **({"credit_note_lines": list(credit_note_lines)} if credit_note_lines else {}),
            },
        )
        db.add(refund)
        db.flush()
        RefundService._audit(db, refund, "billing.refund_requested", actor_user_id)

        if destination != RefundDestination.GATEWAY:
            RefundService._complete_locked(db, refund, payment, invoice)
            return refund

        if mode == "v2" and _is_topup(invoice):
            RefundService._place_hold(db, refund, invoice)
        if defer_gateway:
            refund.next_attempt_at = now
            db.flush()
            return refund

        token = uuid.uuid4().hex
        refund.attempts = 1
        refund.last_attempt_at = now
        refund.claim_token = token
        refund.claim_expires_at = now + CLAIM_TTL
        refund_id = refund.id
        call = RefundService._call_args(db, refund)
        db.commit()
        outcome = RefundService._send(adapter, refund_id, call)
        return RefundService.apply_outcome(db, refund_id, outcome, claim_token=token)

    @staticmethod
    def _by_key(db: Session, key: str) -> Optional[Refund]:
        return db.execute(select(Refund).where(Refund.idempotency_key == key)).scalar_one_or_none()

    @staticmethod
    def _replay(existing: Refund, params: Mapping[str, Any]) -> Refund:
        if (
            int(existing.payment_id) != params["payment_id"]
            or int(existing.amount_cents) != params["amount_cents"]
            or existing.destination != params["destination"]
            or existing.reason != params["reason"]
        ):
            raise RefundError(
                "This idempotency key was already used for a different refund",
                code="idempotency_conflict",
            )
        return existing

    @staticmethod
    def _validate(
        db: Session,
        payment: Payment,
        invoice: Invoice,
        amount_cents: int,
        destination: RefundDestination,
        mode: str,
    ) -> None:
        if payment.status == PaymentStatus.REFUNDED:
            raise RefundError("Payment is already refunded", code="not_refundable")
        if payment.status not in REFUNDABLE_PAYMENT_STATUSES:
            raise RefundError("Only succeeded payments can be refunded", code="not_refundable")
        gateway = _gateway_name(payment)
        if destination == RefundDestination.GATEWAY:
            if gateway in USDT_GATEWAYS:
                raise RefundError(
                    "USDT deposits cannot be refunded through the gateway", code="not_refundable"
                )
            if gateway not in GATEWAY_REFUNDABLE:
                raise RefundError(
                    f"Gateway '{payment.gateway}' does not support gateway refunds",
                    code="not_refundable",
                )
            if not (payment.external_ref or "").strip():
                raise RefundError(
                    "Payment has no gateway reference to refund", code="not_refundable"
                )
        elif gateway == CREDIT_GATEWAY and destination != RefundDestination.CREDIT:
            raise RefundError(
                "Applied credit can only be refunded to credit", code="not_refundable"
            )
        refundable = refundable_cents(db, payment)
        if refundable <= 0:
            raise RefundError("Nothing is left to refund on this payment", code="not_refundable")
        if amount_cents > refundable:
            raise RefundError(
                f"Refund of {amount_cents} exceeds the refundable {refundable}",
                code="amount_exceeds_refundable",
            )
        if mode == "v2":
            return
        if amount_cents != refundable or payment.refunded_cents:
            raise RefundError(
                "Partial refunds need the v2 invoice engine", code="requires_v2"
            )
        if destination == RefundDestination.MANUAL:
            raise RefundError("Manual refunds need the v2 invoice engine", code="requires_v2")
        if destination == RefundDestination.CREDIT and gateway != CREDIT_GATEWAY:
            if gateway in USDT_GATEWAYS:
                raise RefundError(
                    "USDT deposits cannot be refunded through the gateway", code="not_refundable"
                )
            raise RefundError(
                "Refunds to account credit need the v2 invoice engine", code="requires_v2"
            )

    # ------------------------------------------------------------ gateway call

    @staticmethod
    def _call_args(db: Session, refund: Refund) -> dict[str, Any]:
        payment = db.get(Payment, refund.payment_id)
        return {
            "external_ref": payment.external_ref or "",
            "amount_cents": int(refund.amount_cents),
            "currency": refund.currency,
            "idempotency_key": refund.idempotency_key,
            "reason": refund.reason_text,
        }

    @staticmethod
    def _send(adapter: Any, refund_id: int, call: Mapping[str, Any]) -> GatewayRefundResult:
        """Call the gateway for a claimed refund; the caller has committed, so no lock is held."""
        try:
            return adapter.refund_payment(**call)
        except PaymentGatewayError as exc:
            return GatewayRefundResult(
                status=GatewayResultStatus.DECLINED,
                failure_code="gateway_error",
                failure_message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - an unknown answer leaves the refund pending
            logger.warning("Refund %s: gateway call raised %s", refund_id, exc, exc_info=True)
            return GatewayRefundResult(
                status=GatewayResultStatus.TRANSIENT_ERROR,
                failure_code=type(exc).__name__,
                failure_message="The gateway call did not return an answer",
            )

    @staticmethod
    def claim(db: Session, refund_id: int, now: Optional[datetime] = None) -> Optional[str]:
        """Claim a pending refund for one gateway attempt; returns the token or None."""
        now = now or _now()
        token = uuid.uuid4().hex
        result = db.execute(
            update(Refund)
            .where(
                Refund.id == refund_id,
                Refund.status == RefundStatus.PENDING,
                (Refund.claim_token.is_(None)) | (Refund.claim_expires_at < now),
            )
            .values(
                claim_token=token,
                claim_expires_at=now + CLAIM_TTL,
                attempts=Refund.attempts + 1,
                last_attempt_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        return token

    @staticmethod
    def dispatch(db: Session, refund_id: int, *, registry: Any = None) -> Refund:
        """Send a reserved gateway refund now (for example one reserved with ``defer_gateway``)."""
        refund = db.get(Refund, refund_id)
        if refund is None:
            raise RefundError("Refund not found", code="not_found")
        if refund.status != RefundStatus.PENDING or refund.destination != RefundDestination.GATEWAY:
            return refund
        try:
            adapter = _registry(registry).get(refund.gateway)
        except GatewayNotConfiguredError as exc:
            raise RefundError(str(exc), code="gateway_unavailable") from exc
        call = RefundService._call_args(db, refund)
        token = RefundService.claim(db, refund_id)
        if token is None:
            return db.get(Refund, refund_id)
        db.commit()
        outcome = RefundService._send(adapter, refund_id, call)
        return RefundService.apply_outcome(db, refund_id, outcome, claim_token=token)

    @staticmethod
    def apply_outcome(
        db: Session,
        refund_id: int,
        outcome: Optional[GatewayRefundResult],
        *,
        claim_token: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Refund:
        """Record a gateway answer (``None``: no answer) for a pending refund; the caller commits."""
        now = now or _now()
        locked, refund = _lock_refund(db, refund_id)
        if refund.status != RefundStatus.PENDING:
            return refund
        if claim_token is not None and refund.claim_token not in (None, claim_token):
            return refund
        if outcome is not None and outcome.external_ref and not refund.gateway_refund_ref:
            refund.gateway_refund_ref = outcome.external_ref
        if outcome is not None and outcome.status == GatewayResultStatus.SUCCEEDED:
            if outcome.pending:
                RefundService._back_off(refund, now, code="gateway_pending", message=None)
            else:
                RefundService._complete_locked(db, refund, locked.payment, locked.invoice)
            return refund
        if outcome is not None and outcome.status == GatewayResultStatus.DECLINED:
            RefundService._fail_locked(
                db,
                refund,
                failure_code=outcome.failure_code or "gateway_declined",
                failure_message=outcome.failure_message or "Gateway refund failed",
                actor_user_id=None,
            )
            return refund
        RefundService._back_off(
            refund,
            now,
            code=(outcome.failure_code if outcome is not None else None) or "gateway_unknown",
            message=outcome.failure_message if outcome is not None else None,
        )
        return refund

    @staticmethod
    def _back_off(refund: Refund, now: datetime, *, code: str, message: Optional[str]) -> None:
        refund.next_attempt_at = now + RETRY_DELAY
        refund.claim_token = None
        refund.claim_expires_at = None
        refund.failure_code = code
        refund.failure_message = message

    # -------------------------------------------------------------- complete

    @staticmethod
    def complete(db: Session, refund_id: int, *, gateway_refund_ref: Optional[str] = None) -> Refund:
        """Mark a refund succeeded once the gateway has confirmed it; idempotent."""
        locked, refund = _lock_refund(db, refund_id)
        if refund.status == RefundStatus.SUCCEEDED:
            return refund
        if refund.status == RefundStatus.FAILED:
            raise RefundError("A failed refund cannot be completed", code="refund_failed")
        if gateway_refund_ref and not refund.gateway_refund_ref:
            refund.gateway_refund_ref = gateway_refund_ref
        RefundService._complete_locked(db, refund, locked.payment, locked.invoice)
        return refund

    @staticmethod
    def fail(
        db: Session,
        refund_id: int,
        *,
        failure_code: str,
        failure_message: Optional[str] = None,
        actor_user_id: Optional[int] = None,
    ) -> Refund:
        """Record that a pending or review refund did not happen; releases a credit hold."""
        _locked, refund = _lock_refund(db, refund_id)
        if refund.status in OPEN_STATUSES:
            RefundService._fail_locked(
                db,
                refund,
                failure_code=failure_code,
                failure_message=failure_message,
                actor_user_id=actor_user_id,
            )
        return refund

    @staticmethod
    def move_to_review(db: Session, refund_id: int, *, failure_code: str) -> Refund:
        _locked, refund = _lock_refund(db, refund_id)
        if refund.status == RefundStatus.PENDING:
            refund.status = RefundStatus.REVIEW
            refund.claim_token = None
            refund.claim_expires_at = None
            refund.next_attempt_at = None
            refund.failure_code = failure_code
            RefundService._audit(db, refund, "billing.refund_review_required", None)
            db.flush()
        return refund

    @staticmethod
    def record_gateway_refund(
        db: Session,
        payment: Payment,
        *,
        amount_cents: Optional[int],
        reason: RefundReason,
        idempotency_key: str,
        gateway_refund_ref: Optional[str],
        confirmed: bool,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Refund]:
        """Record a refund or dispute the gateway made itself (webhook); the payment is locked.

        Returns None when there is nothing left to refund on the payment.
        """
        existing = RefundService._by_key(db, idempotency_key)
        if existing is not None:
            return existing
        invoice = db.get(Invoice, payment.invoice_id)
        if payment.status not in REFUNDABLE_PAYMENT_STATUSES:
            return None
        refundable = refundable_cents(db, payment)
        if refundable <= 0:
            return None
        mode = invoice_engine_mode(db)
        amount = refundable
        if mode == "v2" and amount_cents is not None and 0 < int(amount_cents) < refundable:
            amount = int(amount_cents)
        refund = Refund(
            payment_id=payment.id,
            invoice_id=invoice.id,
            billing_account_id=account_id_for(db, invoice),
            reseller_id=invoice.reseller_id,
            amount_cents=amount,
            currency=payment.currency,
            destination=RefundDestination.GATEWAY,
            gateway=_gateway_name(payment),
            reason=reason,
            issue_credit_note=mode == "v2" and _default_credit_note(invoice, reason),
            status=RefundStatus.PENDING,
            gateway_refund_ref=gateway_refund_ref,
            idempotency_key=idempotency_key,
            attempts=0,
            requested_at=_now(),
            next_attempt_at=None if confirmed else _now() + RETRY_DELAY,
            refund_metadata={
                **dict(metadata or {}),
                "engine": mode,
                "gateway_amount_cents": amount_cents,
            },
        )
        db.add(refund)
        db.flush()
        RefundService._audit(db, refund, "billing.refund_recorded", None)
        if confirmed:
            RefundService._complete_locked(db, refund, payment, invoice)
        return refund

    # ------------------------------------------------------- shared internals

    @staticmethod
    def _complete_locked(db: Session, refund: Refund, payment: Payment, invoice: Invoice) -> None:
        if refund.status == RefundStatus.SUCCEEDED:
            return
        now = _now()
        mode = (refund.refund_metadata or {}).get("engine") or invoice_engine_mode(db)
        refund.status = RefundStatus.SUCCEEDED
        refund.completed_at = now
        refund.claim_token = None
        refund.claim_expires_at = None
        refund.next_attempt_at = None
        refund.failure_code = None
        refund.failure_message = None

        payment.refunded_cents = int(payment.refunded_cents or 0) + int(refund.amount_cents)
        if payment.refunded_cents > int(payment.amount_cents):
            raise RefundError("Refunds would exceed the payment amount", code="amount_exceeds_refundable")
        payment.status = (
            PaymentStatus.REFUNDED
            if payment.refunded_cents == int(payment.amount_cents)
            else PaymentStatus.PARTIALLY_REFUNDED
        )
        payment.refunded_at = now
        metadata = dict(refund.refund_metadata or {})
        legacy_reason = metadata.get("legacy_reason") or refund.reason.value
        payment_metadata = {**(payment.payment_metadata or {}), "reversal_type": legacy_reason}
        if metadata.get("event_id") and metadata.get("legacy_reason") in {"refund", "dispute"}:
            payment_metadata["gateway_event_id"] = metadata["event_id"]
        if metadata.get("legacy_reason") == "admin_refund":
            payment_metadata["admin_refund"] = {
                "event_id": metadata.get("event_id"),
                "reason": refund.reason_text,
                "admin_user_id": refund.requested_by_user_id,
                **(
                    {"gateway_refund_ref": refund.gateway_refund_ref}
                    if refund.destination == RefundDestination.GATEWAY
                    else {}
                ),
            }
        payment_metadata["refund_ids"] = [
            *[int(i) for i in (payment.payment_metadata or {}).get("refund_ids", [])],
            int(refund.id),
        ]
        payment.payment_metadata = payment_metadata

        if mode == "v2":
            adjustment = RefundService._complete_v2(db, refund, payment, invoice)
        else:
            RefundService._legacy_side_effects(db, refund, payment, invoice, legacy_reason)
            adjustment = None
        RefundService._audit(db, refund, "billing.refund_succeeded", refund.requested_by_user_id)
        db.flush()

        from app.services.billing import settlement

        settlement.fire(
            db,
            settlement.PAYMENT_REFUNDED,
            settlement.PaymentRefunded(refund=refund, credit_note=adjustment),
        )
        if mode == "v2":
            from app.services.invoice_service import InvoiceService

            cause = "dispute" if refund.reason == RefundReason.DISPUTE else "refund"
            InvoiceService.recompute(db, invoice, cause=cause)
        else:
            set_invoice_status(db, invoice, InvoiceStatus.FAILED, cause=legacy_reason)
        db.flush()

    @staticmethod
    def _legacy_side_effects(
        db: Session, refund: Refund, payment: Payment, invoice: Invoice, legacy_reason: str
    ) -> None:
        """Ledger effects of a full refund before the invoice engine switch."""
        from app.services.credit_ledger_service import CreditLedgerService

        metadata = refund.refund_metadata or {}
        event_id = metadata.get("event_id") or refund.idempotency_key
        gateway = refund.gateway
        if _gateway_name(payment) == CREDIT_GATEWAY:
            debit = RefundService._credit_payment_debit(db, payment)
            entry = CreditLedgerService.reverse(
                db,
                invoice.reseller_id,
                debit.id,
                description=(
                    refund.reason_text
                    or f"Admin refund of credit payment on invoice {invoice.invoice_number}"
                ),
                idempotency_key=event_id,
                metadata={"reason": legacy_reason, "admin_user_id": refund.requested_by_user_id},
                created_by_user_id=refund.requested_by_user_id,
            )
            refund.ledger_entry_id = entry.id
            return
        if _is_topup(invoice):
            topup = db.execute(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.payment_id == payment.id,
                    CreditLedgerEntry.entry_type == CreditLedgerEntryType.TOPUP,
                )
            ).scalar_one_or_none()
            if topup is not None and invoice.reseller_id is not None:
                entry = CreditLedgerService.reverse(
                    db,
                    invoice.reseller_id,
                    topup.id,
                    description=f"Gateway {legacy_reason} for top-up invoice {invoice.invoice_number}",
                    idempotency_key=f"{gateway}-{legacy_reason}:{event_id}",
                    metadata={"gateway_event_id": event_id, "reason": legacy_reason},
                )
                refund.ledger_entry_id = entry.id
            return
        if invoice.reseller_id is None:
            AuditService.log(
                db,
                actor_user_id=None,
                billing_account_id=invoice.billing_account_id,
                action="billing.payment_reversed",
                resource_type="invoice",
                resource_id=str(invoice.id),
                metadata={
                    "reason": legacy_reason,
                    "gateway": gateway,
                    "gateway_event_id": event_id,
                    "payment_id": payment.id,
                    "refund_id": refund.id,
                    "amount_cents": int(payment.amount_cents),
                    "currency": payment.currency,
                },
            )
            return
        entry = CreditLedgerService.debit(
            db,
            invoice.reseller_id,
            int(refund.amount_cents),
            description=f"Gateway {legacy_reason} for invoice {invoice.invoice_number}",
            invoice_id=invoice.id,
            payment_id=payment.id,
            service_id=invoice.service_id,
            reference_type=f"gateway_{legacy_reason}",
            reference_id=event_id,
            idempotency_key=f"{gateway}-{legacy_reason}:{event_id}",
            metadata={"gateway_event_id": event_id},
            allow_negative_balance=True,
        )
        refund.ledger_entry_id = entry.id

    @staticmethod
    def _credit_payment_debit(db: Session, payment: Payment) -> CreditLedgerEntry:
        ledger_entry_id = (payment.payment_metadata or {}).get("ledger_entry_id")
        debit = db.get(CreditLedgerEntry, int(ledger_entry_id)) if ledger_entry_id is not None else None
        if debit is None:
            debit = db.execute(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.payment_id == payment.id,
                    CreditLedgerEntry.entry_type == CreditLedgerEntryType.CHARGE,
                )
            ).scalar_one_or_none()
        if debit is None:
            raise RefundError(
                "Credit payment has no ledger debit to reverse", code="missing_ledger_entry"
            )
        return debit

    @staticmethod
    def _complete_v2(db: Session, refund: Refund, payment: Payment, invoice: Invoice) -> Any:
        from app.services.credit_ledger_service import CreditLedgerService

        account_id = refund.billing_account_id
        if refund.reason == RefundReason.DISPUTE:
            RefundService._dispute_policy(db, refund, invoice)
        elif refund.destination == RefundDestination.CREDIT and not _is_topup(invoice):
            if account_id is None:
                raise RefundError("The invoice has no billing account to credit", code="no_account")
            entry = CreditLedgerService.credit_account(
                db,
                int(account_id),
                int(refund.amount_cents),
                description=f"Refund of payment {payment.id} on invoice {invoice.invoice_number}",
                invoice_id=invoice.id,
                payment_id=payment.id,
                reference_type="refund",
                reference_id=str(refund.id),
                idempotency_key=f"refund:{refund.id}",
                metadata={"refund_id": refund.id, "reason": refund.reason.value},
                created_by_user_id=refund.requested_by_user_id,
            )
            refund.ledger_entry_id = getattr(entry, "id", None)
        elif _is_topup(invoice) and refund.destination == RefundDestination.GATEWAY:
            hold_id = (refund.refund_metadata or {}).get("hold_entry_id")
            if hold_id is None:
                # A refund the gateway made itself has no hold; the deposit is debited now.
                hold_id = RefundService._place_hold(db, refund, invoice, allow_negative=True)
            refund.ledger_entry_id = hold_id
        adjustment = None
        if refund.issue_credit_note:
            from app.services.invoice_adjustment_service import InvoiceAdjustmentService

            lines = (refund.refund_metadata or {}).get("credit_note_lines")
            adjustment = InvoiceAdjustmentService.issue_for_refund(db, refund, lines=lines)
            refund.adjustment_id = getattr(adjustment, "id", None)
        return adjustment

    @staticmethod
    def _dispute_policy(db: Session, refund: Refund, invoice: Invoice) -> None:
        from app.services.billing.settings_access import billing_setting
        from app.services.credit_ledger_service import CreditLedgerService

        key = (
            "billing_dispute_policy_reseller"
            if invoice.reseller_id is not None
            else "billing_dispute_policy_retail"
        )
        policy = str(billing_setting(db, key))
        refund.refund_metadata = {**(refund.refund_metadata or {}), "dispute_policy": policy}
        if _is_topup(invoice):
            RefundService._place_hold(db, refund, invoice, allow_negative=True)
            refund.ledger_entry_id = (refund.refund_metadata or {}).get("hold_entry_id")
            return
        if policy == "debit_balance":
            CreditLedgerService.apply_to_invoice(
                db,
                invoice,
                amount_cents=int(refund.amount_cents),
                actor_user_id=None,
                idempotency_key=f"dispute-apply:{refund.id}",
                allow_negative_balance=True,
            )

    @staticmethod
    def _place_hold(
        db: Session, refund: Refund, invoice: Invoice, *, allow_negative: bool = False
    ) -> Optional[int]:
        """Debit the refunded deposit from the balance (``refund-hold:<id>``)."""
        from app.services.credit_ledger_service import CreditLedgerService, CreditLedgerError

        if refund.billing_account_id is None:
            raise RefundError("The invoice has no billing account", code="no_account")
        try:
            entry = CreditLedgerService.debit_account(
                db,
                int(refund.billing_account_id),
                int(refund.amount_cents),
                description=f"Refund of top-up invoice {invoice.invoice_number}",
                invoice_id=invoice.id,
                payment_id=refund.payment_id,
                reference_type="refund",
                reference_id=str(refund.id),
                idempotency_key=f"refund-hold:{refund.id}",
                metadata={"refund_id": refund.id},
                allow_negative_balance=allow_negative,
            )
        except CreditLedgerError as exc:
            raise RefundError(
                "The balance is too low to refund this top-up", code="insufficient_balance"
            ) from exc
        hold_id = getattr(entry, "id", None)
        refund.refund_metadata = {**(refund.refund_metadata or {}), "hold_entry_id": hold_id}
        db.flush()
        return hold_id

    @staticmethod
    def _fail_locked(
        db: Session,
        refund: Refund,
        *,
        failure_code: str,
        failure_message: Optional[str],
        actor_user_id: Optional[int],
    ) -> None:
        from app.services.credit_ledger_service import CreditLedgerService

        refund.status = RefundStatus.FAILED
        refund.failure_code = failure_code
        refund.failure_message = failure_message
        refund.completed_at = _now()
        refund.claim_token = None
        refund.claim_expires_at = None
        refund.next_attempt_at = None
        hold_id = (refund.refund_metadata or {}).get("hold_entry_id")
        if hold_id is not None:
            CreditLedgerService.credit_account(
                db,
                int(refund.billing_account_id),
                int(refund.amount_cents),
                description=f"Release of refund hold {refund.id}",
                invoice_id=refund.invoice_id,
                payment_id=refund.payment_id,
                reference_type="refund",
                reference_id=str(refund.id),
                idempotency_key=f"refund-hold-release:{refund.id}",
                metadata={"refund_id": refund.id},
            )
        RefundService._audit(db, refund, "billing.refund_failed", actor_user_id)
        db.flush()

    @staticmethod
    def _audit(db: Session, refund: Refund, action: str, actor_user_id: Optional[int]) -> None:
        AuditService.log(
            db,
            actor_user_id=actor_user_id,
            billing_account_id=refund.billing_account_id,
            action=action,
            resource_type="refund",
            resource_id=str(refund.id),
            metadata={
                "payment_id": refund.payment_id,
                "invoice_id": refund.invoice_id,
                "amount_cents": int(refund.amount_cents),
                "currency": refund.currency,
                "destination": refund.destination.value,
                "reason": refund.reason.value,
                "status": refund.status.value,
                "gateway": refund.gateway,
                "gateway_refund_ref": refund.gateway_refund_ref,
                "failure_code": refund.failure_code,
            },
        )
