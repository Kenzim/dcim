from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest

from app.models.reseller import (
    PayPalPendingSetup,
    Payment,
    Reseller,
    ResellerPaymentMethod,
)
from app.models.user import User
from app.services.invoice_service import InvoiceService
from app.services.payments.base import (
    GatewayChargeResult,
    GatewayPaymentMethod,
    GatewayResultStatus,
    GatewayVaultSetup,
    PaymentGatewayError,
    PaymentMethodOwnershipError,
)
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.payments.registry import PaymentGatewayRegistry


class FakeGateway:
    def __init__(self, name, outcomes=None, call_order=None):
        self.name = name
        self.outcomes = defaultdict(list, outcomes or {})
        self.call_order = call_order if call_order is not None else []
        self.complete_calls = 0

    def create_vault_setup(self, *, reseller_id, customer_ref=None):
        return GatewayVaultSetup(
            external_ref=f"SETUP-{reseller_id}",
            approval_url=f"https://paypal.test/approve/{reseller_id}",
            customer_ref=customer_ref or f"customer_{reseller_id}",
        )

    def complete_vault_setup(self, *, setup_token_ref):
        self.complete_calls += 1
        return self.get_payment_method(method_ref="VAULT-1")

    def get_payment_method(self, *, method_ref):
        return GatewayPaymentMethod(
            external_ref=method_ref,
            customer_ref="customer_1",
            method_type="paypal",
            label="PayPal (payer@example.test)",
            brand="paypal",
            metadata={
                "merchant_customer_id": "rackflow-reseller-1",
                "payer_email": "payer@example.test",
            },
        )

    def detach_payment_method(self, **_kwargs):
        return None

    def charge_off_session(self, **kwargs):
        self.call_order.append((self.name, kwargs["method_ref"]))
        return self.outcomes[kwargs["method_ref"]].pop(0)


def _reseller(db_session, suffix):
    reseller = Reseller(
        user=User(
            username=f"paypal-{suffix}",
            email=f"paypal-{suffix}@example.test",
            is_reseller=True,
        )
    )
    db_session.add(reseller)
    db_session.flush()
    return reseller


def test_setup_ownership_expiry_safe_storage_and_idempotency(db_session):
    first = _reseller(db_session, "first")
    second = _reseller(db_session, "second")
    gateway = FakeGateway("paypal")
    registry = PaymentGatewayRegistry()
    registry.register(gateway)
    orchestrator = PaymentOrchestrator(registry)

    setup = orchestrator.create_paypal_vault_setup(db_session, first)
    pending = db_session.query(PayPalPendingSetup).one()
    with pytest.raises(PaymentMethodOwnershipError):
        orchestrator.complete_paypal_vault_setup(
            db_session,
            second,
            setup_token_ref=setup.external_ref,
        )

    pending.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(PaymentGatewayError, match="expired"):
        orchestrator.complete_paypal_vault_setup(
            db_session,
            first,
            setup_token_ref=setup.external_ref,
        )

    pending.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    method = orchestrator.complete_paypal_vault_setup(
        db_session,
        first,
        setup_token_ref=setup.external_ref,
        tier=2,
    )
    replay = orchestrator.complete_paypal_vault_setup(
        db_session,
        first,
        setup_token_ref=setup.external_ref,
    )

    assert replay.id == method.id
    assert gateway.complete_calls == 1
    assert method.provider == "paypal"
    assert method.provider_method_ref == "VAULT-1"
    assert method.provider_customer_ref == "customer_1"
    assert method.label == "PayPal (payer@example.test)"
    assert method.brand == "paypal"
    assert method.last4 is None
    assert setup.external_ref not in {
        method.provider_method_ref,
        method.provider_customer_ref,
        method.label,
    }
    assert pending.payment_method_id == method.id
    assert pending.consumed_at is not None


def test_stripe_paypal_tier_order_decline_fallback_and_capture_ref(db_session):
    reseller = _reseller(db_session, "fallback")
    reseller.stripe_customer_ref = "cus_1"
    invoice = InvoiceService.create_deploy(
        db_session,
        reseller_id=reseller.id,
        amount_cents=1500,
        description="Mixed gateway fallback",
    )
    stripe_method = ResellerPaymentMethod(
        reseller_id=reseller.id,
        provider="stripe",
        method_type="card",
        provider_customer_ref="cus_1",
        provider_method_ref="pm_decline",
        tier=1,
    )
    paypal_method = ResellerPaymentMethod(
        reseller_id=reseller.id,
        provider="paypal",
        method_type="paypal",
        provider_customer_ref="customer_1",
        provider_method_ref="VAULT-success",
        tier=2,
    )
    db_session.add_all([stripe_method, paypal_method])
    db_session.flush()
    call_order = []
    stripe = FakeGateway(
        "stripe",
        {
            "pm_decline": [
                GatewayChargeResult(
                    status=GatewayResultStatus.DECLINED,
                    external_ref="pi_declined",
                )
            ]
        },
        call_order,
    )
    paypal = FakeGateway(
        "paypal",
        {
            "VAULT-success": [
                GatewayChargeResult(
                    status=GatewayResultStatus.SUCCEEDED,
                    external_ref="CAPTURE-success",
                )
            ]
        },
        call_order,
    )
    registry = PaymentGatewayRegistry()
    registry.register(stripe)
    registry.register(paypal)

    result = PaymentOrchestrator(registry).fund_invoice(
        db_session, reseller, invoice
    )

    assert result.funded is True
    assert call_order == [
        ("stripe", "pm_decline"),
        ("paypal", "VAULT-success"),
    ]
    paypal_payment = (
        db_session.query(Payment)
        .filter(Payment.gateway == "paypal")
        .one()
    )
    assert paypal_payment.external_ref == "CAPTURE-success"
