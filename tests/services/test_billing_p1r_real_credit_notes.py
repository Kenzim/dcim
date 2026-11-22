"""P1-R against the real P1-N credit note service (recompute is still P1-I's stub, so it is recorded)."""

from __future__ import annotations

from sqlalchemy import select

from app.models.invoice_adjustment import InvoiceAdjustment
from app.models.refund import RefundStatus
from app.models.reseller import InvoiceStatus
from app.services.refund_service import RefundService
from app.services.payments.base import GatewayRefundResult, GatewayResultStatus
from tests.services.test_billing_p1n_adjustments import account, make_invoice, make_payment, rec  # noqa: F401
from tests.services.test_billing_p1r_refund_service import FakeGateway, _registry


def test_partial_refunds_issue_real_credit_notes_with_cumulative_tax(db_session, account, rec):  # noqa: F811
    invoice = make_invoice(db_session, account, amount=1000, status=InvoiceStatus.PAID, tax_cents=167)
    payment = make_payment(db_session, invoice, 1000)
    payment.external_ref = "pi_p1r_real"
    db_session.commit()
    refunds = []
    for index, part in enumerate((333, 333, 334)):
        gateway = FakeGateway(outcome=GatewayRefundResult(status=GatewayResultStatus.SUCCEEDED, external_ref=f"re_real_{index}"))
        refund = RefundService.request(
            db_session, payment_id=payment.id, amount_cents=part, destination="gateway",
            reason="customer_request", actor_user_id=None, idempotency_key=f"p1r-real-{index}",
            registry=_registry(gateway),
        )
        db_session.commit()
        assert refund.status == RefundStatus.SUCCEEDED
        refunds.append(refund)

    notes = [db_session.get(InvoiceAdjustment, refund.adjustment_id) for refund in refunds]
    assert [note.idempotency_key for note in notes] == [f"refund-cn:{refund.id}" for refund in refunds]
    assert [note.total_cents for note in notes] == [333, 333, 334]
    assert [note.subtotal_cents + note.tax_cents for note in notes] == [333, 333, 334]
    assert sum(note.tax_cents for note in notes) == 167
    assert all(note.tax_cents >= 0 for note in notes)
    assert rec.recomputes == [(invoice.id, "refund")] * 3
    db_session.refresh(payment)
    assert payment.refunded_cents == 1000
    assert db_session.scalar(select(InvoiceAdjustment.id).where(InvoiceAdjustment.invoice_id == invoice.id).order_by(InvoiceAdjustment.id.desc())) == notes[-1].id
