"""Admin payment refund service coverage."""

from app.models.reseller import (
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
)
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User
from app.services.credit_ledger_service import CreditLedgerService
from app.services.payments.base import GatewayRefundResult, GatewayResultStatus
from app.services.payments.refunds import PaymentRefundService
from app.services.payments.registry import PaymentGatewayRegistry


class FakeRefundGateway:
    name = "stripe"

    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or GatewayRefundResult(
            status=GatewayResultStatus.SUCCEEDED,
            external_ref="re_test_1",
        )

    def refund_payment(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


def _reseller(db_session, suffix):
    reseller = Reseller(
        user=User(
            username=f"refund-{suffix}",
            email=f"refund-{suffix}@example.test",
            is_reseller=True,
        )
    )
    db_session.add(reseller)
    db_session.flush()
    return reseller


def test_credit_payment_refund_reverses_ledger(db_session):
    reseller = _reseller(db_session, "credit")
    CreditLedgerService.credit(
        db_session,
        reseller.id,
        5000,
        idempotency_key="seed-credit-refund",
    )
    invoice = Invoice(
        invoice_number=910001,
        reseller_id=reseller.id,
        purpose=InvoicePurpose.DEPLOY_CHARGE,
        status=InvoiceStatus.PAID,
        amount_cents=2000,
        currency="USD",
    )
    db_session.add(invoice)
    db_session.flush()
    payment = Payment(
        invoice_id=invoice.id,
        gateway="credit",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=2000,
        currency="USD",
        external_ref=f"credit:{invoice.id}:1",
    )
    db_session.add(payment)
    db_session.flush()
    debit = CreditLedgerService.debit(
        db_session,
        reseller.id,
        2000,
        description="Credit allocation",
        invoice_id=invoice.id,
        payment_id=payment.id,
        idempotency_key=f"invoice-credit:{invoice.id}:{payment.id}",
    )
    payment.payment_metadata = {
        "source": "reseller_credit",
        "ledger_entry_id": debit.id,
    }
    db_session.commit()

    refunded = PaymentRefundService().refund(
        db_session, payment.id, reason="Admin credit refund", admin_user_id=1
    )
    db_session.commit()
    db_session.refresh(reseller)
    db_session.refresh(invoice)
    db_session.refresh(refunded)

    assert refunded.status == PaymentStatus.REFUNDED
    assert refunded.refunded_at is not None
    assert invoice.status == InvoiceStatus.FAILED
    assert reseller.cached_balance_cents == 5000


def test_stripe_gateway_refund_reverses_topup(db_session):
    reseller = _reseller(db_session, "stripe")
    invoice = Invoice(
        invoice_number=910002,
        reseller_id=reseller.id,
        purpose=InvoicePurpose.CREDIT_TOPUP,
        status=InvoiceStatus.PAID,
        amount_cents=3000,
        currency="USD",
    )
    db_session.add(invoice)
    db_session.flush()
    payment = Payment(
        invoice_id=invoice.id,
        gateway="stripe",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=3000,
        currency="USD",
        external_ref="pi_refund_test",
    )
    db_session.add(payment)
    db_session.flush()
    CreditLedgerService.credit(
        db_session,
        reseller.id,
        3000,
        description="Paid top-up",
        invoice_id=invoice.id,
        payment_id=payment.id,
        reference_type="topup_invoice",
        reference_id=str(invoice.id),
        idempotency_key=f"topup-invoice:{invoice.id}",
    )
    db_session.commit()

    gateway = FakeRefundGateway()
    registry = PaymentGatewayRegistry()
    registry.register(gateway)
    refunded = PaymentRefundService(registry).refund(
        db_session, payment.id, reason="Duplicate top-up"
    )
    db_session.commit()
    db_session.refresh(reseller)
    db_session.refresh(invoice)
    db_session.refresh(refunded)

    assert len(gateway.calls) == 1
    assert gateway.calls[0]["external_ref"] == "pi_refund_test"
    assert gateway.calls[0]["amount_cents"] == 3000
    assert refunded.status == PaymentStatus.REFUNDED
    assert invoice.status == InvoiceStatus.FAILED
    assert reseller.cached_balance_cents == 0


def test_admin_credit_refund_api_and_service_links(
    client, db_session, test_admin_user
):
    from tests.api.test_reseller_admin import _admin_headers, _create_reseller

    headers = _admin_headers(client)
    created = _create_reseller(client, headers, "refund-api")
    reseller = db_session.get(Reseller, created["id"])
    CreditLedgerService.credit(
        db_session,
        reseller.id,
        4000,
        idempotency_key="refund-api-seed",
    )
    owner = User(
        username="refund-api-client",
        email="refund-api-client@example.com",
        reseller_id=reseller.id,
    )
    service = Service(
        name="refund-api-service",
        owner_user=owner,
        service_type=ServiceType.VM,
        status=ServiceStatus.ACTIVE,
    )
    db_session.add(service)
    db_session.flush()
    invoice = Invoice(
        invoice_number=910003,
        reseller_id=reseller.id,
        service_id=service.id,
        purpose=InvoicePurpose.DEPLOY_CHARGE,
        status=InvoiceStatus.PAID,
        amount_cents=1500,
        currency="USD",
        description="Deploy charge",
    )
    db_session.add(invoice)
    db_session.flush()
    payment = Payment(
        invoice_id=invoice.id,
        gateway="credit",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=1500,
        currency="USD",
        external_ref=f"credit:{invoice.id}:1",
    )
    db_session.add(payment)
    db_session.flush()
    debit = CreditLedgerService.debit(
        db_session,
        reseller.id,
        1500,
        description="Credit allocation",
        invoice_id=invoice.id,
        payment_id=payment.id,
        service_id=service.id,
        idempotency_key=f"invoice-credit:{invoice.id}:{payment.id}",
    )
    payment.payment_metadata = {
        "source": "reseller_credit",
        "ledger_entry_id": debit.id,
    }
    db_session.commit()

    response = client.post(
        f"/api/admin/resellers/payments/{payment.id}/refund",
        headers=headers,
        json={"reason": "Courtesy refund"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "refunded"
    assert body["refundable"] is False

    detail = client.get(
        f"/api/admin/resellers/invoices/{invoice.id}", headers=headers
    )
    assert detail.status_code == 200
    assert detail.json()["service"]["id"] == service.id
    assert detail.json()["service"]["name"] == "refund-api-service"
    assert detail.json()["status"] == "failed"
    assert detail.json()["reseller"]["username"] == created["user"]["username"]

    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 4000
