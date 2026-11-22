"""Invoice funding across wallet credit and ordered saved payment methods."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dao.reseller_dao import ResellerDAO
from app.models.reseller import (
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    PayPalPendingSetup,
    Payment,
    PaymentStatus,
    Reseller,
    ResellerChargePreference,
    ResellerPaymentMethod,
)
from app.services.billing_locks import BillingLocks
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_service import (
    TERMINAL_V2,
    InvoiceError,
    InvoiceService,
    engine_mode,
)
from app.services.payments.base import (
    GatewayPaymentMethod,
    GatewayResultStatus,
    GatewayVaultSetup,
    PaymentGatewayError,
    PaymentMethodOwnershipError,
)
from app.services.payments.registry import (
    PaymentGatewayRegistry,
    payment_gateway_registry,
)


logger = logging.getLogger(__name__)

# A replaced (failed) attempt whose money arrives anyway is recorded and then
# flagged by settlement as an overpayment; a refunded payment never succeeds again.
SUCCESS_ALLOWED_FROM = frozenset(
    {PaymentStatus.PENDING, PaymentStatus.FAILED, PaymentStatus.SUCCEEDED}
)


@dataclass(frozen=True)
class InvoiceFundingResult:
    funded: bool
    allocated_cents: int
    remaining_cents: int
    pending_action: bool = False
    client_secret: Optional[str] = None
    payment_id: Optional[int] = None

    def __bool__(self) -> bool:
        return self.funded


class PaymentOrchestrator:
    def __init__(
        self, registry: PaymentGatewayRegistry = payment_gateway_registry
    ) -> None:
        self.registry = registry

    def ensure_stripe_customer(
        self, db: Session, reseller: Reseller
    ) -> str:
        locked = ResellerDAO.get_for_update(db, reseller.id)
        if locked is None:
            raise InvoiceError("Reseller not found")
        if locked.stripe_customer_ref:
            return locked.stripe_customer_ref
        gateway = self.registry.get("stripe")
        customer = gateway.create_customer(
            reseller_id=locked.id,
            email=locked.user.email if locked.user is not None else None,
        )
        locked.stripe_customer_ref = customer.external_ref
        db.flush()
        return customer.external_ref

    def create_stripe_setup_intent(
        self, db: Session, reseller: Reseller
    ):
        customer_ref = self.ensure_stripe_customer(db, reseller)
        return self.registry.get("stripe").create_setup_intent(
            customer_ref=customer_ref
        )

    def register_stripe_method(
        self,
        db: Session,
        reseller: Reseller,
        *,
        method_ref: str,
        tier: int = 1,
        label: Optional[str] = None,
    ) -> ResellerPaymentMethod:
        if type(tier) is not int or tier < 0:
            raise ValueError("tier must be a non-negative integer")
        customer_ref = self.ensure_stripe_customer(db, reseller)
        method = self.registry.get("stripe").get_payment_method(
            method_ref=method_ref
        )
        if method.customer_ref != customer_ref:
            raise PaymentMethodOwnershipError(
                "Payment method does not belong to this reseller"
            )
        saved = db.execute(
            select(ResellerPaymentMethod).where(
                ResellerPaymentMethod.provider == "stripe",
                ResellerPaymentMethod.provider_method_ref
                == method.external_ref,
            )
        ).scalar_one_or_none()
        if saved is not None and saved.reseller_id != reseller.id:
            raise PaymentMethodOwnershipError(
                "Payment method belongs to another reseller"
            )
        if saved is None:
            saved = ResellerPaymentMethod(
                reseller_id=reseller.id,
                provider="stripe",
                provider_method_ref=method.external_ref,
            )
            db.add(saved)
        saved.provider_customer_ref = customer_ref
        saved.method_type = method.method_type
        saved.label = (label or method.label or "Stripe payment method")[:255]
        saved.brand = method.brand[:64] if method.brand else None
        saved.last4 = method.last4[-4:] if method.last4 else None
        saved.tier = tier
        saved.enabled = True
        db.flush()
        return saved

    def create_paypal_vault_setup(
        self, db: Session, reseller: Reseller
    ) -> GatewayVaultSetup:
        gateway = self.registry.get("paypal")
        create_setup = getattr(gateway, "create_vault_setup", None)
        if create_setup is None:
            raise PaymentGatewayError(
                "PayPal vault setup is not supported by this gateway"
            )
        customer_ref = db.scalar(
            select(ResellerPaymentMethod.provider_customer_ref)
            .where(
                ResellerPaymentMethod.reseller_id == reseller.id,
                ResellerPaymentMethod.provider == "paypal",
                ResellerPaymentMethod.provider_customer_ref.is_not(None),
            )
            .order_by(ResellerPaymentMethod.id)
            .limit(1)
        )
        setup = create_setup(
            reseller_id=reseller.id,
            customer_ref=customer_ref,
        )
        pending = PayPalPendingSetup(
            reseller_id=reseller.id,
            setup_token_ref=setup.external_ref,
            provider_customer_ref=setup.customer_ref,
            expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        db.add(pending)
        db.flush()
        return setup

    def complete_paypal_vault_setup(
        self,
        db: Session,
        reseller: Reseller,
        *,
        setup_token_ref: str,
        tier: int = 1,
        label: Optional[str] = None,
    ) -> ResellerPaymentMethod:
        if type(tier) is not int or tier < 0:
            raise ValueError("tier must be a non-negative integer")
        pending = self._paypal_pending_for_update(
            db, reseller.id, setup_token_ref
        )
        completed = self._completed_paypal_setup_method(
            db, reseller.id, pending
        )
        if completed is not None:
            return completed
        self._validate_paypal_setup_expiry(pending)

        gateway = self.registry.get("paypal")
        complete_setup = getattr(gateway, "complete_vault_setup", None)
        if complete_setup is None:
            raise PaymentGatewayError(
                "PayPal vault completion is not supported by this gateway"
            )
        exchanged = complete_setup(setup_token_ref=setup_token_ref)
        method = gateway.get_payment_method(
            method_ref=exchanged.external_ref
        )
        self._validate_paypal_method(
            reseller.id,
            pending,
            exchanged.external_ref,
            method,
        )
        saved = self._save_paypal_method(
            db,
            reseller.id,
            method,
            tier=tier,
            label=label,
        )
        pending.payment_method_id = saved.id
        pending.consumed_at = datetime.now(timezone.utc)
        db.flush()
        return saved

    @staticmethod
    def _paypal_pending_for_update(
        db: Session, reseller_id: int, setup_token_ref: str
    ) -> PayPalPendingSetup:
        pending = db.execute(
            select(PayPalPendingSetup)
            .where(PayPalPendingSetup.setup_token_ref == setup_token_ref)
            .with_for_update()
        ).scalar_one_or_none()
        if pending is None or pending.reseller_id != reseller_id:
            raise PaymentMethodOwnershipError("PayPal setup token not found")
        return pending

    @staticmethod
    def _completed_paypal_setup_method(
        db: Session,
        reseller_id: int,
        pending: PayPalPendingSetup,
    ) -> Optional[ResellerPaymentMethod]:
        if pending.consumed_at is None:
            return None
        saved = (
            db.get(ResellerPaymentMethod, pending.payment_method_id)
            if pending.payment_method_id is not None
            else None
        )
        if saved is None or saved.reseller_id != reseller_id:
            raise PaymentGatewayError(
                "Completed PayPal setup has no saved payment method"
            )
        return saved

    @staticmethod
    def _validate_paypal_setup_expiry(
        pending: PayPalPendingSetup,
    ) -> None:
        expires_at = pending.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise PaymentGatewayError("PayPal setup token has expired")

    @staticmethod
    def _validate_paypal_method(
        reseller_id: int,
        pending: PayPalPendingSetup,
        exchanged_method_ref: str,
        method: GatewayPaymentMethod,
    ) -> None:
        if method.external_ref != exchanged_method_ref:
            raise PaymentGatewayError("PayPal returned an unexpected payment token")
        expected_merchant_customer = f"rackflow-reseller-{reseller_id}"
        merchant_customer = method.metadata.get("merchant_customer_id")
        if (
            merchant_customer is not None
            and merchant_customer != expected_merchant_customer
        ):
            raise PaymentMethodOwnershipError(
                "PayPal payment token belongs to another reseller"
            )
        if (
            pending.provider_customer_ref
            and method.customer_ref
            and pending.provider_customer_ref != method.customer_ref
        ):
            raise PaymentMethodOwnershipError(
                "PayPal payment token customer does not match setup"
            )

    @staticmethod
    def _save_paypal_method(
        db: Session,
        reseller_id: int,
        method: GatewayPaymentMethod,
        *,
        tier: int,
        label: Optional[str],
    ) -> ResellerPaymentMethod:
        saved = db.execute(
            select(ResellerPaymentMethod).where(
                ResellerPaymentMethod.provider == "paypal",
                ResellerPaymentMethod.provider_method_ref
                == method.external_ref,
            )
        ).scalar_one_or_none()
        if saved is not None and saved.reseller_id != reseller_id:
            raise PaymentMethodOwnershipError(
                "PayPal payment token belongs to another reseller"
            )
        if saved is None:
            saved = ResellerPaymentMethod(
                reseller_id=reseller_id,
                provider="paypal",
                provider_method_ref=method.external_ref,
            )
            db.add(saved)
        saved.provider_customer_ref = method.customer_ref
        saved.method_type = method.method_type
        saved.label = (label or method.label or "PayPal")[:255]
        saved.brand = method.brand[:64] if method.brand else "paypal"
        saved.last4 = None
        saved.tier = tier
        saved.enabled = True
        db.flush()
        return saved

    def detach_method(
        self, db: Session, reseller: Reseller, method_id: int
    ) -> None:
        method = db.get(ResellerPaymentMethod, method_id)
        if method is None or method.reseller_id != reseller.id:
            raise PaymentMethodOwnershipError("Payment method not found")
        self.registry.get(method.provider).detach_payment_method(
            method_ref=method.provider_method_ref
        )
        db.delete(method)
        db.flush()

    @staticmethod
    def _methods(
        db: Session,
        reseller: Reseller,
        selected_method_id: Optional[int],
    ) -> list[ResellerPaymentMethod]:
        methods = ResellerDAO.list_payment_methods(
            db, reseller.id, enabled_only=True
        )
        if selected_method_id is None:
            return methods
        selected = [
            method for method in methods if method.id == selected_method_id
        ]
        if not selected:
            raise PaymentMethodOwnershipError("Payment method not found")
        return selected

    @staticmethod
    def _result(
        db: Session,
        invoice: Invoice,
        *,
        pending_action: bool = False,
        client_secret: Optional[str] = None,
        payment_id: Optional[int] = None,
    ) -> InvoiceFundingResult:
        allocated = InvoiceService.allocated_cents(db, invoice.id)
        return InvoiceFundingResult(
            funded=allocated == invoice.amount_cents,
            allocated_cents=allocated,
            remaining_cents=max(0, invoice.amount_cents - allocated),
            pending_action=pending_action,
            client_secret=client_secret,
            payment_id=payment_id,
        )

    def fund_invoice(
        self,
        db: Session,
        reseller: Reseller,
        invoice: Invoice,
        *,
        payment_method_id: Optional[int] = None,
    ) -> InvoiceFundingResult:
        locked_reseller, locked_invoice = self._lock_funding_rows(
            db, reseller.id, invoice.id
        )
        initial = self._result(db, locked_invoice)
        if initial.funded:
            InvoiceService.mark_paid_if_fully_allocated(db, locked_invoice.id)
            return initial
        pending = self._pending_funding_result(db, locked_invoice)
        if pending is not None:
            return pending

        methods = self._methods(db, locked_reseller, payment_method_id)
        if (
            payment_method_id is not None
            or locked_invoice.purpose == InvoicePurpose.CREDIT_TOPUP
        ):
            return self._charge_methods(
                db, locked_reseller, locked_invoice, methods, initial.remaining_cents
            )
        return self._fund_by_preference(
            db,
            locked_reseller,
            locked_invoice,
            methods,
            initial.remaining_cents,
        )

    @staticmethod
    def _lock_funding_rows(
        db: Session, reseller_id: int, invoice_id: int
    ) -> tuple[Reseller, Invoice]:
        """Lock the reseller, its billing account, then the invoice (design 6.3)."""
        reseller, _account = BillingLocks.reseller_and_account(db, reseller_id)
        if reseller is None:
            raise InvoiceError("Reseller not found")
        invoice = InvoiceService.get_for_update(db, invoice_id)
        if invoice is None or invoice.reseller_id != reseller.id:
            raise InvoiceError("Invoice not found")
        if invoice.status == InvoiceStatus.VOID or (
            invoice.status in TERMINAL_V2 and engine_mode(db) == "v2"
        ):
            # Refused before any gateway call: a terminal v2 invoice takes no payment.
            raise InvoiceError("Invoice cannot be paid in its current state")
        return reseller, invoice

    def _pending_funding_result(
        self, db: Session, invoice: Invoice
    ) -> Optional[InvoiceFundingResult]:
        if invoice.status != InvoiceStatus.PENDING_ACTION:
            return None
        pending_payment = CreditLedgerService.live_pending_payment(
            db, CreditLedgerService.lock_invoice_payments(db, invoice.id)
        )
        if pending_payment is None:
            return None
        return self._result(
            db,
            invoice,
            pending_action=True,
            payment_id=pending_payment.id,
        )

    def _fund_by_preference(
        self,
        db: Session,
        reseller: Reseller,
        invoice: Invoice,
        methods: Iterable[ResellerPaymentMethod],
        remaining_cents: int,
    ) -> InvoiceFundingResult:
        if reseller.charge_preference == ResellerChargePreference.PAYMENT_FIRST:
            first = self._charge_methods(
                db,
                reseller,
                invoice,
                methods,
                remaining_cents,
            )
            if first.funded or first.pending_action:
                return first
            self._apply_credit(db, reseller, invoice, first.remaining_cents)
        else:
            self._apply_credit(db, reseller, invoice, remaining_cents)

        after_credit = self._result(db, invoice)
        if after_credit.funded:
            InvoiceService.mark_paid_if_fully_allocated(db, invoice.id)
            return after_credit
        return self._charge_methods(
            db,
            reseller,
            invoice,
            methods,
            after_credit.remaining_cents,
        )

    def _apply_credit(
        self,
        db: Session,
        reseller: Reseller,
        invoice: Invoice,
        requested_cents: int,
    ) -> Optional[Payment]:
        available = CreditLedgerService.available_cents(db, reseller)
        amount = min(available, requested_cents)
        if amount <= 0:
            return None
        payments = CreditLedgerService.lock_invoice_payments(db, invoice.id)
        # The rule of apply_to_invoice: no credit while a gateway payment is pending.
        if CreditLedgerService.live_pending_payment(db, payments) is not None:
            return None
        return CreditLedgerService.allocate_to_locked_invoice(
            db,
            invoice,
            reseller=reseller,
            amount_cents=amount,
            created_by_user_id=reseller.user_id,
            locked_payments=payments,
        )

    def _charge_methods(
        self,
        db: Session,
        reseller: Reseller,
        invoice: Invoice,
        methods: Iterable[ResellerPaymentMethod],
        amount_cents: int,
    ) -> InvoiceFundingResult:
        if amount_cents <= 0:
            InvoiceService.mark_paid_if_fully_allocated(db, invoice.id)
            return self._result(db, invoice)
        for method in methods:
            result = self._charge_method(
                db, reseller, invoice, method, amount_cents
            )
            if result.funded or result.pending_action:
                return result
        return self._result(db, invoice)

    def _charge_method(
        self,
        db: Session,
        reseller: Reseller,
        invoice: Invoice,
        method: ResellerPaymentMethod,
        amount_cents: int,
    ) -> InvoiceFundingResult:
        if (
            method.provider == "stripe"
            and (
                method.provider_customer_ref is None
                or method.provider_customer_ref != reseller.stripe_customer_ref
            )
        ):
            return self._result(db, invoice)
        gateway = self.registry.maybe_get(method.provider)
        if gateway is None:
            return self._result(db, invoice)
        # Stable key (invoice + method + amount + purpose) so a gateway success
        # followed by a local failure cannot double-charge on retry.
        idempotency_key = (
            f"invoice-{invoice.id}-{method.provider}-{method.id}-"
            f"{amount_cents}-{invoice.purpose.value if hasattr(invoice.purpose, 'value') else invoice.purpose}"
        )
        outcome = gateway.charge_off_session(
            customer_ref=method.provider_customer_ref,
            method_ref=method.provider_method_ref,
            amount_cents=amount_cents,
            currency=invoice.currency,
            idempotency_key=idempotency_key,
            metadata={
                "rackflow_invoice_id": str(invoice.id),
                "rackflow_reseller_id": str(reseller.id),
            },
        )
        status = self._payment_status(outcome.status)
        payment = Payment(
            invoice_id=invoice.id,
            gateway=method.provider,
            status=status,
            amount_cents=amount_cents,
            currency=invoice.currency,
            external_ref=outcome.external_ref,
            failure_code=outcome.failure_code,
            failure_message=outcome.failure_message,
            payment_metadata={
                "saved_method_id": method.id,
                "provider_method_ref": method.provider_method_ref,
                "idempotency_key": idempotency_key,
            },
            processed_at=(
                datetime.now(timezone.utc)
                if status != PaymentStatus.PENDING
                else None
            ),
        )
        db.add(payment)
        db.flush()
        if status == PaymentStatus.SUCCEEDED:
            self.finalize_success(db, payment)
            return self._result(db, invoice, payment_id=payment.id)
        if status == PaymentStatus.PENDING:
            InvoiceService.transition(
                db, invoice, InvoiceStatus.PENDING_ACTION, cause="payment_requires_action"
            )
            db.flush()
            return self._result(
                db,
                invoice,
                pending_action=True,
                client_secret=outcome.client_secret,
                payment_id=payment.id,
            )
        return self._result(db, invoice, payment_id=payment.id)

    @staticmethod
    def _payment_status(gateway_status: GatewayResultStatus) -> PaymentStatus:
        if gateway_status == GatewayResultStatus.SUCCEEDED:
            return PaymentStatus.SUCCEEDED
        if gateway_status == GatewayResultStatus.REQUIRES_ACTION:
            return PaymentStatus.PENDING
        return PaymentStatus.FAILED

    @staticmethod
    def refuse_late_success(
        db: Session, payment: Payment, *, source: str, event_id: Optional[str] = None
    ) -> bool:
        """Record and refuse a success report for a payment that may no longer succeed.

        Returns True when the payment's status does not allow success (it was
        refunded or reversed); the report is then logged and audited only.
        """
        if payment.status in SUCCESS_ALLOWED_FROM:
            return False
        from app.services.audit_service import AuditService

        invoice = db.get(Invoice, payment.invoice_id)
        logger.warning(
            "Ignoring a success report for payment %s, which is %s",
            payment.id,
            payment.status.value,
        )
        AuditService.log(
            db,
            actor_user_id=None,
            billing_account_id=invoice.billing_account_id if invoice is not None else None,
            action="billing.late_success_ignored",
            resource_type="payment",
            resource_id=str(payment.id),
            metadata={
                "status": payment.status.value,
                "gateway": payment.gateway,
                "source": source,
                "gateway_event_id": event_id,
                "invoice_id": payment.invoice_id,
                "amount_cents": int(payment.amount_cents),
            },
        )
        db.flush()
        return True

    @staticmethod
    def _credit_topup(db: Session, invoice: Invoice, **keywords) -> None:
        """Credit a paid top-up invoice to its reseller's or retail account's balance."""
        if invoice.reseller_id is not None:
            CreditLedgerService.credit(
                db, invoice.reseller_id, invoice.amount_cents, **keywords
            )
            return
        if invoice.billing_account_id is None:
            raise InvoiceError("Top-up invoice has no billing account")
        CreditLedgerService.credit_account(
            db, invoice.billing_account_id, invoice.amount_cents, **keywords
        )

    @staticmethod
    def finalize_success(db: Session, payment: Payment) -> Invoice:
        """Record a gateway-confirmed payment and settle its invoice.

        Only a pending, failed or just-recorded succeeded payment may succeed; a
        refunded (reversed or disputed) payment is left as it is.
        """
        if PaymentOrchestrator.refuse_late_success(db, payment, source="finalize_success"):
            return db.get(Invoice, payment.invoice_id)
        payment.status = PaymentStatus.SUCCEEDED
        payment.processed_at = payment.processed_at or datetime.now(timezone.utc)
        payment.failure_code = None
        payment.failure_message = None
        db.flush()
        invoice = InvoiceService.mark_paid_if_fully_allocated(
            db, payment.invoice_id, payment=payment
        )
        if (
            invoice.status == InvoiceStatus.PAID
            and invoice.purpose == InvoicePurpose.CREDIT_TOPUP
        ):
            PaymentOrchestrator._credit_topup(
                db,
                invoice,
                description=f"Paid top-up invoice {invoice.invoice_number}",
                invoice_id=invoice.id,
                payment_id=payment.id,
                reference_type="topup_invoice",
                reference_id=str(invoice.id),
                idempotency_key=f"topup-invoice:{invoice.id}",
            )
            if invoice.reseller_id is None:
                db.flush()
                return invoice
            from app.services.notification_service import (
                NotificationEvent,
                NotificationService,
            )

            try:
                NotificationService.enqueue(
                    db,
                    reseller=invoice.reseller,
                    event=NotificationEvent.TOPUP_RECEIVED,
                    idempotency_key=f"topup-received:{invoice.id}",
                    data={
                        "invoice_id": invoice.id,
                        "invoice_number": invoice.invoice_number,
                        "amount_cents": invoice.amount_cents,
                        "currency": invoice.currency,
                    },
                )
            except ValueError:
                pass
        db.flush()
        return invoice
