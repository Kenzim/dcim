from collections import defaultdict

import pytest

from app.models.reseller import (
    CreditLedgerEntry,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
    ResellerChargePreference,
    ResellerPaymentMethod,
)
from app.models.user import User
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_service import InvoiceService
from app.services.payments.base import (
    GatewayChargeResult,
    GatewayResultStatus,
)
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.payments.registry import PaymentGatewayRegistry


class FakeGateway:
    name = "stripe"

    def __init__(self, outcomes):
        self.outcomes = defaultdict(list, outcomes)
        self.calls = []

    def charge_off_session(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcomes[kwargs["method_ref"]].pop(0)


def _reseller(db_session, *, balance=0, preference="credit_first"):
    reseller = Reseller(
        user=User(
            username=f"pay-{preference}-{balance}",
            email=f"pay-{preference}-{balance}@example.com",
            is_reseller=True,
        ),
        charge_preference=ResellerChargePreference(preference),
    )
    db_session.add(reseller)
    db_session.flush()
    reseller.stripe_customer_ref = f"cus_{reseller.id}"
    if balance:
        CreditLedgerService.credit(
            db_session,
            reseller.id,
            balance,
            idempotency_key=f"seed:{reseller.id}",
        )
    return reseller


def _method(db_session, reseller, ref, tier):
    method = ResellerPaymentMethod(
        reseller_id=reseller.id,
        provider="stripe",
        method_type="card",
        provider_customer_ref=f"cus_{reseller.id}",
        provider_method_ref=ref,
        label=ref,
        tier=tier,
    )
    db_session.add(method)
    db_session.flush()
    return method


def _orchestrator(gateway):
    registry = PaymentGatewayRegistry()
    registry.register(gateway)
    return PaymentOrchestrator(registry)


def _outcome(status, ref):
    return GatewayChargeResult(status=status, external_ref=ref)


def test_invoice_numbers_are_monotonic_and_allocations_gate_paid(db_session):
    reseller = _reseller(db_session)
    invoices = [
        InvoiceService.create_topup(
            db_session, reseller_id=reseller.id, amount_cents=500 + index
        )
        for index in range(3)
    ]
    assert [row.invoice_number for row in invoices] == [
        invoices[0].invoice_number,
        invoices[0].invoice_number + 1,
        invoices[0].invoice_number + 2,
    ]

    db_session.add(
        Payment(
            invoice_id=invoices[0].id,
            gateway="stripe",
            status=PaymentStatus.SUCCEEDED,
            amount_cents=499,
            currency="USD",
            external_ref="pi_partial",
        )
    )
    db_session.flush()
    InvoiceService.mark_paid_if_fully_allocated(db_session, invoices[0].id)
    assert invoices[0].status == InvoiceStatus.OPEN


def test_credit_first_charges_only_shortfall_without_granting_credit(db_session):
    reseller = _reseller(db_session, balance=400)
    method = _method(db_session, reseller, "pm_one", 1)
    invoice = InvoiceService.create_deploy(
        db_session,
        reseller_id=reseller.id,
        amount_cents=1000,
        description="Deploy",
    )
    gateway = FakeGateway(
        {"pm_one": [_outcome(GatewayResultStatus.SUCCEEDED, "pi_shortfall")]}
    )

    result = _orchestrator(gateway).fund_invoice(
        db_session, reseller, invoice
    )

    assert result.funded is True
    assert gateway.calls[0]["amount_cents"] == 600
    assert reseller.cached_balance_cents == 0
    assert [row.amount_cents for row in invoice.payments] == [400, 600]
    assert method.id == invoice.payments[1].payment_metadata["saved_method_id"]


def test_payment_first_matrix_and_tier_order(db_session):
    reseller = _reseller(
        db_session, balance=400, preference="payment_first"
    )
    _method(db_session, reseller, "pm_second", 2)
    _method(db_session, reseller, "pm_first", 1)
    invoice = InvoiceService.create_deploy(
        db_session,
        reseller_id=reseller.id,
        amount_cents=1000,
        description="Deploy",
    )
    gateway = FakeGateway(
        {
            "pm_first": [
                _outcome(GatewayResultStatus.DECLINED, "pi_declined"),
                _outcome(GatewayResultStatus.SUCCEEDED, "pi_remaining"),
            ],
            "pm_second": [
                _outcome(GatewayResultStatus.DECLINED, "pi_declined_2"),
            ],
        }
    )

    result = _orchestrator(gateway).fund_invoice(
        db_session, reseller, invoice
    )

    assert result.funded is True
    assert [call["method_ref"] for call in gateway.calls] == [
        "pm_first",
        "pm_second",
        "pm_first",
    ]
    assert [call["amount_cents"] for call in gateway.calls] == [1000, 1000, 600]
    assert reseller.cached_balance_cents == 0


def test_payment_first_success_preserves_wallet_balance(db_session):
    reseller = _reseller(
        db_session, balance=400, preference="payment_first"
    )
    _method(db_session, reseller, "pm_full", 1)
    invoice = InvoiceService.create_deploy(
        db_session,
        reseller_id=reseller.id,
        amount_cents=1000,
        description="Deploy",
    )
    gateway = FakeGateway(
        {"pm_full": [_outcome(GatewayResultStatus.SUCCEEDED, "pi_full")]}
    )

    _orchestrator(gateway).fund_invoice(db_session, reseller, invoice)

    assert reseller.cached_balance_cents == 400
    assert invoice.status == InvoiceStatus.PAID
    assert (
        db_session.query(CreditLedgerEntry)
        .filter(CreditLedgerEntry.invoice_id == invoice.id)
        .count()
        == 0
    )


def test_topup_credits_exactly_once_and_sca_is_not_success(db_session):
    reseller = _reseller(db_session)
    method = _method(db_session, reseller, "pm_topup", 1)
    topup = InvoiceService.create_topup(
        db_session, reseller_id=reseller.id, amount_cents=2500
    )
    success = FakeGateway(
        {"pm_topup": [_outcome(GatewayResultStatus.SUCCEEDED, "pi_topup")]}
    )
    orchestrator = _orchestrator(success)

    result = orchestrator.fund_invoice(
        db_session, reseller, topup, payment_method_id=method.id
    )
    PaymentOrchestrator.finalize_success(db_session, topup.payments[0])

    assert result.funded is True
    assert reseller.cached_balance_cents == 2500
    assert len(reseller.ledger_entries) == 1

    sca_invoice = InvoiceService.create_deploy(
        db_session,
        reseller_id=reseller.id,
        amount_cents=1000,
        description="SCA",
    )
    sca = FakeGateway(
        {
            "pm_topup": [
                GatewayChargeResult(
                    status=GatewayResultStatus.REQUIRES_ACTION,
                    external_ref="pi_sca",
                    client_secret="pi_sca_secret_safe_response",
                )
            ]
        }
    )
    sca_result = _orchestrator(sca).fund_invoice(
        db_session,
        reseller,
        sca_invoice,
        payment_method_id=method.id,
    )
    replay_result = _orchestrator(sca).fund_invoice(
        db_session,
        reseller,
        sca_invoice,
        payment_method_id=method.id,
    )
    assert sca_result.pending_action is True
    assert sca_result.funded is False
    assert replay_result.pending_action is True
    assert len(sca.calls) == 1
    assert sca_invoice.status == InvoiceStatus.PENDING_ACTION
    assert sca_invoice.payments[0].status == PaymentStatus.PENDING
    assert "client_secret" not in sca_invoice.payments[0].payment_metadata


@pytest.mark.parametrize("invalid", [0, -1, 1.5, True, "500"])
def test_invoice_rejects_non_positive_integer_cents(db_session, invalid):
    reseller = _reseller(db_session)
    with pytest.raises(ValueError):
        InvoiceService.create_topup(
            db_session, reseller_id=reseller.id, amount_cents=invalid
        )
