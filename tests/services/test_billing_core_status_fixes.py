"""Core invoice status review fixes (Opus review of integration 1eb4f0c, ledger note dbdba08e)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.commerce_audit import UserAuditEvent
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    ServiceBillingStatus,
)
from app.services.billing_shadow import BillingShadowService
from app.services.invoice_service import InvoiceError, InvoiceService
from app.services.payments.base import GatewayChargeResult, GatewayResultStatus
from app.services.settings_store import set_setting
from tests.services.test_billing_p1i_invoice_engine import _v2
from tests.services.test_billing_w0e_review_fixes import _assert_lock_order, lock_calls  # noqa: F401
from tests.services.test_invoice_payment_services import (
    FakeGateway,
    _method,
    _orchestrator,
    _reseller,
)

S = InvoiceStatus


def _shadow(db):
    set_setting(db, "billing_invoice_engine", "shadow")
    db.flush()


def _legacy(db):
    set_setting(db, "billing_invoice_engine", "legacy")
    db.flush()


def _set_status(db, invoice, status):
    """Test setup only: put an invoice in a status no public path reaches directly."""
    db.execute(
        Invoice.__table__.update().where(Invoice.__table__.c.id == invoice.id).values(status=status)
    )
    db.flush()
    db.refresh(invoice)


def _requires_action(ref):
    return GatewayChargeResult(
        status=GatewayResultStatus.REQUIRES_ACTION, external_ref=ref, client_secret=f"{ref}_secret"
    )


# ------------------------------------------------------------------ B1


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_b1_three_d_secure_charge_on_a_failed_invoice_records_the_pending_payment(db_session, mode):
    set_setting(db_session, "billing_invoice_engine", mode)
    reseller = _reseller(db_session, preference="payment_first")
    _method(db_session, reseller, "pm_3ds", 1)
    invoice = InvoiceService.create_deploy(
        db_session, reseller_id=reseller.id, amount_cents=1000, description="Deploy"
    )
    InvoiceService.transition(db_session, invoice, S.FAILED, cause="refund")
    gateway = FakeGateway({"pm_3ds": [_requires_action("pi_3ds_failed")]})

    result = _orchestrator(gateway).fund_invoice(db_session, reseller, invoice)

    assert result.pending_action is True
    assert len(gateway.calls) == 1
    assert invoice.status == S.PENDING_ACTION
    payment = db_session.get(Payment, result.payment_id)
    assert (payment.status, payment.external_ref) == (PaymentStatus.PENDING, "pi_3ds_failed")


@pytest.mark.parametrize("terminal", [S.REFUNDED, S.FAILED, S.VOID])
def test_b1_v2_refuses_to_fund_a_terminal_invoice_before_any_gateway_call(db_session, terminal):
    _v2(db_session)
    reseller = _reseller(db_session, preference="payment_first")
    _method(db_session, reseller, "pm_terminal", 1)
    invoice = InvoiceService.create_deploy(
        db_session, reseller_id=reseller.id, amount_cents=1000, description="Deploy"
    )
    _set_status(db_session, invoice, terminal)
    gateway = FakeGateway({"pm_terminal": [_requires_action("pi_never")]})

    with pytest.raises(InvoiceError, match="cannot be paid"):
        _orchestrator(gateway).fund_invoice(db_session, reseller, invoice)

    assert gateway.calls == []
    assert db_session.scalar(select(Payment.id).where(Payment.invoice_id == invoice.id)) is None
