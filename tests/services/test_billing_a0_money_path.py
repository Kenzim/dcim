"""Billing A0 regression tests: retail money path, single settlement, invoice lines, coupons, order cancel."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO, BillingProfileDAO
from app.dao.service_dao import ServiceDAO
from app.models.commerce_coupon_tax import (
    Coupon,
    CouponAppliesTo,
    CouponDuration,
    CouponRedemption,
    TaxRate,
)
from app.models.commerce_email import EmailMessage
from app.models.commerce_invoice_ext import InvoiceLine
from app.models.commerce_order import Order, OrderStatus
from app.models.product_catalog import Product, ProductFamily
from app.models.reseller import (
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
)
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.storefront import (
    FrontendProduct,
    FrontendProductVisibility,
    PricePlan,
    PricePlanCycle,
    PricePlanInterval,
    PricePlanPricingModel,
    ProductOption,
    ProductOptionType,
    ProductOptionValue,
)
from app.models.user import User
from app.plugin_host import events as plugin_events
from app.services.billing import settlement
from app.services.billing.coupons import record_coupon_usage
from app.services.billing.manual_payments import record_manual_payment
from app.services.billing.order_refunds import cancel_order
from app.services.checkout_service import CheckoutError, CheckoutService
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_service import InvoiceService
from app.services.payments.base import (
    GatewayPaymentIntent,
    GatewayWebhookAction,
    NormalizedGatewayWebhookEvent,
)
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.payments.registry import PaymentGatewayRegistry
from app.services.payments.retail_card import RetailCardPaymentError, start_card_payment
from app.services.payments.webhooks import PaymentWebhookService


@pytest.fixture(autouse=True)
def _retail(monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)


@pytest.fixture
def emitted(monkeypatch):
    """Record every plugin event emission while still running the real emit()."""
    calls: list[tuple[str, dict]] = []
    original = plugin_events.emit

    def recording(db, name, payload, *args, **kwargs):
        calls.append((name, dict(payload)))
        return original(db, name, payload, *args, **kwargs)

    monkeypatch.setattr(plugin_events, "emit", recording)
    return calls


@pytest.fixture
def provisioned(monkeypatch):
    names: list[str] = []

    def fake_create(db, req, actor):
        names.append(req.name)
        return ServiceDAO.create_vm(
            db,
            name=req.name,
            owner_user_id=req.owner_user_id,
            status=ServiceStatus.PENDING,
            provisioning_source=ProvisioningSource.BILLING,
        )

    monkeypatch.setattr("app.services.checkout_service.ProvisioningService.create", fake_create)
    return names


@pytest.fixture
def provisioning_fails(monkeypatch):
    def failing(db, req, actor):
        raise RuntimeError("no capacity")

    monkeypatch.setattr("app.services.checkout_service.ProvisioningService.create", failing)


def _events(calls, name: str, key: str, value: int) -> list[dict]:
    return [payload for event, payload in calls if event == name and payload.get(key) == value]


def _user(db: Session, prefix: str = "a0") -> User:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    user = User(username=name, email=f"{name}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _account(db: Session, prefix: str = "a0"):
    user = _user(db, prefix)
    return user, BillingAccountDAO.ensure_client_account(db, user.id)


def _product(
    db: Session,
    *,
    setup_cents: int = 0,
    price_cents: int = 1000,
    option_delta: int = 0,
    pricing_model: PricePlanPricingModel = PricePlanPricingModel.RECURRING,
) -> tuple[FrontendProduct, PricePlan]:
    suffix = uuid.uuid4().hex[:8]
    family = ProductFamily(
        name=f"A0 {suffix}", code=f"a0-{suffix}", service_type="vm", enabled=True,
        defaults={}, constraints={},
    )
    db.add(family)
    db.flush()
    catalog = Product(
        family_id=family.id, name=f"A0 Prod {suffix}", code=f"a0p-{suffix}", enabled=True, overrides={},
    )
    db.add(catalog)
    db.flush()
    fp = FrontendProduct(
        name=f"A0 {suffix}", slug=f"a0-{suffix}", short_description="d", description_md="m",
        description_html="<p>m</p>", product_id=catalog.id, service_type="vm", enabled=True,
        visibility=FrontendProductVisibility.PUBLIC, sort_order=0, features={}, specs={},
        require_discord=False,
    )
    db.add(fp)
    db.flush()
    plan = PricePlan(
        frontend_product_id=fp.id, currency="USD", pricing_model=pricing_model,
        setup_cents=setup_cents, trial_days=0, enabled=True, name="Monthly",
    )
    db.add(plan)
    db.flush()
    db.add(
        PricePlanCycle(
            price_plan_id=plan.id, interval=PricePlanInterval.MONTHLY, price_cents=price_cents, enabled=True,
        )
    )
    if option_delta:
        option = ProductOption(
            frontend_product_id=fp.id, code="ram", name="RAM", required=False, option_type=ProductOptionType.SELECT,
        )
        db.add(option)
        db.flush()
        db.add(ProductOptionValue(option_id=option.id, code="big", name="Big", price_delta_cents=option_delta))
    db.commit()
    db.refresh(fp)
    db.refresh(plan)
    return fp, plan


def _place(db, account, user, product, plan, *, cycle="monthly", **kwargs) -> Order:
    order = CheckoutService.place_order(
        db,
        account=account,
        user_id=user.id,
        frontend_product_id=product.id,
        price_plan_id=plan.id,
        cycle_interval=cycle,
        checkout_nonce=uuid.uuid4().hex,
        terms_version="1",
        **kwargs,
    )
    db.commit()
    return order


def _open_invoice(db: Session, account, amount: int = 700) -> Invoice:
    invoice = InvoiceService.create(
        db,
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        amount_cents=amount,
        description="A0 test invoice",
    )
    db.commit()
    return invoice


class _FakeCardGateway:
    name = "stripe"

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.status = "requires_payment_method"
        self.event = None
        self.cancelled: list[str] = []

    def create_payment_intent(self, *, amount_cents, currency, idempotency_key, metadata):
        self.created.append({"amount": amount_cents, "key": idempotency_key, "metadata": dict(metadata)})
        ref = f"pi_test_{len(self.created)}"
        return GatewayPaymentIntent(
            external_ref=ref, client_secret=f"{ref}_secret", status=self.status,
            amount_cents=amount_cents, currency=currency,
        )

    def retrieve_payment_intent(self, *, external_ref):
        status = "canceled" if external_ref in self.cancelled else self.status
        return GatewayPaymentIntent(
            external_ref=external_ref, client_secret=f"{external_ref}_secret", status=status,
        )

    def cancel_payment_intent(self, *, external_ref):
        self.cancelled.append(external_ref)
        return GatewayPaymentIntent(external_ref=external_ref, client_secret=None, status="canceled")

    def parse_webhook(self, payload, headers):
        return self.event


def _registry(gateway) -> PaymentGatewayRegistry:
    registry = PaymentGatewayRegistry()
    registry.register(gateway)
    return registry


def _succeeded(ref: str, amount: int) -> NormalizedGatewayWebhookEvent:
    return NormalizedGatewayWebhookEvent(
        event_id=f"evt_{uuid.uuid4().hex[:10]}",
        event_type="payment_intent.succeeded",
        action=GatewayWebhookAction.PAYMENT_COMPLETED,
        external_ref=ref,
        amount_cents=amount,
        currency="USD",
    )


def _deliver(db: Session, gateway: _FakeCardGateway, event) -> bool:
    handled = PaymentWebhookService.process(db, gateway="stripe", event=event, raw_payload=b"{}")
    db.commit()
    db.expire_all()
    return handled


# Items 1 and 2: card payment settles through the one settlement function.


def test_card_payment_webhook_settles_invoice_and_fulfils_order(db_session, provisioned, emitted):
    user, account = _account(db_session)
    product, plan = _product(db_session, setup_cents=500, price_cents=1000)
    order = _place(db_session, account, user, product, plan)
    invoice = db_session.get(Invoice, order.invoice_id)
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert invoice.amount_cents == 1500

    gateway = _FakeCardGateway()
    started = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    db_session.commit()
    assert started.client_secret == "pi_test_1_secret"
    payment = db_session.get(Payment, started.payment_id)
    assert (payment.status, payment.external_ref, payment.amount_cents) == (PaymentStatus.PENDING, "pi_test_1", 1500)
    assert gateway.created[0]["key"].startswith(f"rackflow-invoice-{invoice.id}-card-1")

    again = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    db_session.commit()
    assert again.payment_id == payment.id
    assert again.client_secret == "pi_test_1_secret"
    assert len(gateway.created) == 1
    assert db_session.scalar(select(func.count(Payment.id)).where(Payment.invoice_id == invoice.id)) == 1

    assert _deliver(db_session, gateway, _succeeded("pi_test_1", 1500)) is True
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
    assert db_session.get(Payment, payment.id).status == PaymentStatus.SUCCEEDED
    assert db_session.get(Order, order.id).status == OrderStatus.ACTIVE
    assert len(provisioned) == 1
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1
    assert len(_events(emitted, "order.paid", "order_id", order.id)) == 1
    paid_payload = _events(emitted, "invoice.paid", "invoice_id", invoice.id)[0]
    assert paid_payload == {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "billing_account_id": account.id,
        "order_id": order.id,
    }
    assert db_session.scalar(
        select(func.count(EmailMessage.id)).where(EmailMessage.idempotency_key == f"invoice:{invoice.id}:paid")
    ) == 1

    # A replay under a new event id must not settle or fulfil again.
    _deliver(db_session, gateway, _succeeded("pi_test_1", 1500))
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1
    assert len(provisioned) == 1


def test_card_payment_in_flight_returns_processing_without_new_intent(db_session):
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account)
    gateway = _FakeCardGateway()
    start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    db_session.commit()
    gateway.status = "processing"
    second = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    assert second.status == "processing"
    assert second.client_secret is None
    assert len(gateway.created) == 1


def test_card_payment_amount_change_replaces_attempt(db_session):
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account)
    gateway = _FakeCardGateway()
    first = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    invoice.amount_cents = 900
    db_session.commit()
    second = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    db_session.commit()
    assert second.payment_id != first.payment_id
    assert db_session.get(Payment, first.payment_id).status == PaymentStatus.FAILED
    assert gateway.cancelled == [first.payment_intent_id]
    assert second.amount_cents == 900
    assert gateway.created[0]["key"] != gateway.created[1]["key"]


def test_card_payment_amount_change_while_in_flight_creates_no_new_charge(db_session):
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account)
    gateway = _FakeCardGateway()
    first = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    invoice.amount_cents = 900
    db_session.commit()
    gateway.status = "processing"
    second = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    assert (second.status, second.payment_id) == ("processing", first.payment_id)
    assert len(gateway.created) == 1
    assert gateway.cancelled == []


def test_late_success_on_replaced_attempt_is_recorded_and_flagged(db_session, emitted):
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 700)
    gateway = _FakeCardGateway()
    first = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    invoice.amount_cents = 900
    db_session.commit()
    second = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    db_session.commit()
    _deliver(db_session, gateway, _succeeded(second.payment_intent_id, 900))
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
    # The first attempt's money arrives anyway (for example a race with the cancel).
    _deliver(db_session, gateway, _succeeded(first.payment_intent_id, 700))
    late = db_session.get(Payment, first.payment_id)
    assert late.status == PaymentStatus.SUCCEEDED
    assert late.payment_metadata["refund_review"]["overpayment"]["amount_cents"] == 700
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1


def test_retail_refund_event_records_reversal_without_reseller_ledger(db_session):
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 700)
    gateway = _FakeCardGateway()
    started = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    db_session.commit()
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, 700))
    refund = NormalizedGatewayWebhookEvent(
        event_id=f"evt_{uuid.uuid4().hex[:10]}",
        event_type="charge.refunded",
        action=GatewayWebhookAction.PAYMENT_REVERSED,
        external_ref=started.payment_intent_id,
        amount_cents=700,
        currency="USD",
    )
    _deliver(db_session, gateway, refund)
    assert db_session.get(Payment, started.payment_id).status == PaymentStatus.REFUNDED


def test_payment_on_voided_retail_invoice_is_flagged(db_session, emitted):
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 700)
    gateway = _FakeCardGateway()
    started = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    db_session.get(Invoice, invoice.id).status = InvoiceStatus.VOID
    db_session.commit()
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, 700))
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.VOID
    assert "payment_on_void_invoice" in db_session.get(Payment, started.payment_id).payment_metadata["refund_review"]
    assert _events(emitted, "invoice.paid", "invoice_id", invoice.id) == []


def test_card_payment_rejects_other_accounts_invoice(db_session):
    _, owner = _account(db_session)
    _, other = _account(db_session)
    invoice = _open_invoice(db_session, owner)
    with pytest.raises(RetailCardPaymentError):
        start_card_payment(db_session, invoice, billing_account_id=other.id, registry=_registry(_FakeCardGateway()))


def _client_login(client, user: User) -> None:
    resp = client.post("/api/client/login", json={"username": user.username, "password": "password123"})
    if resp.status_code != 200:
        resp = client.post("/api/users/login", json={"username": user.username, "password": "password123"})
    assert resp.status_code == 200, resp.text


def test_client_pay_endpoint_returns_manual_without_gateway(client, db_session, monkeypatch):
    from app.services.payments import retail_card

    monkeypatch.setattr(retail_card, "payment_gateway_registry", PaymentGatewayRegistry())
    user, account = _account(db_session, "payapi")
    invoice = _open_invoice(db_session, account)
    _client_login(client, user)
    resp = client.post(f"/api/client/commerce/invoices/{invoice.id}/pay")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "manual"


def test_client_pay_endpoint_creates_one_pending_payment(client, db_session, monkeypatch):
    from app.services.payments import retail_card

    gateway = _FakeCardGateway()
    monkeypatch.setattr(retail_card, "payment_gateway_registry", _registry(gateway))
    user, account = _account(db_session, "payapi")
    invoice = _open_invoice(db_session, account)
    _client_login(client, user)
    first = client.post(f"/api/client/commerce/invoices/{invoice.id}/pay")
    second = client.post(f"/api/client/commerce/invoices/{invoice.id}/pay")
    assert first.status_code == 200, first.text
    assert first.json()["client_secret"] == "pi_test_1_secret"
    assert second.json()["payment_id"] == first.json()["payment_id"]
    assert len(gateway.created) == 1
    db_session.expire_all()
    payment = db_session.get(Payment, first.json()["payment_id"])
    assert (payment.status, payment.external_ref) == (PaymentStatus.PENDING, "pi_test_1")


def test_settlement_fires_subscribers_once_in_order(db_session):
    seen: list[str] = []
    settlement.subscribe_invoice_paid("test_a", lambda db, ev: seen.append("a"))
    settlement.subscribe_invoice_paid("test_b", lambda db, ev: seen.append("b"))
    try:
        _, account = _account(db_session)
        invoice = _open_invoice(db_session, account, 300)
        record_manual_payment(db_session, invoice.id, reason="t", actor_user_id=None)
        settlement.settle_invoice(db_session, invoice.id)
        assert seen == ["a", "b"]
        names = settlement.invoice_paid_subscribers()
        assert names.index("coupon_usage") < names.index("publish_invoice_paid") < names.index("fulfil_order")
    finally:
        settlement.unsubscribe_invoice_paid("test_a")
        settlement.unsubscribe_invoice_paid("test_b")


def test_failing_side_effect_subscriber_does_not_undo_settlement(db_session, emitted):
    def boom(db, event):
        raise RuntimeError("email down")

    settlement.subscribe_invoice_paid("test_boom", boom)
    try:
        _, account = _account(db_session)
        invoice = _open_invoice(db_session, account, 300)
        record_manual_payment(db_session, invoice.id, reason="t", actor_user_id=None)
        db_session.commit()
        assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
        assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1
    finally:
        settlement.unsubscribe_invoice_paid("test_boom")


# Items 2 and 3: every settlement path publishes invoice.paid exactly once.


def test_admin_mark_paid_records_manual_payment_and_one_event(db_session, provisioned, emitted):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1200)
    order = _place(db_session, account, user, product, plan)
    invoice, payment = record_manual_payment(db_session, order.invoice_id, reason="wire received", actor_user_id=42)
    db_session.commit()
    assert invoice.status == InvoiceStatus.PAID
    assert (payment.gateway, payment.status, payment.amount_cents) == ("manual", PaymentStatus.SUCCEEDED, 1200)
    assert payment.payment_metadata["reason"] == "wire received"
    assert payment.payment_metadata["actor_user_id"] == 42
    assert db_session.get(Order, order.id).status == OrderStatus.ACTIVE
    _, none = record_manual_payment(db_session, invoice.id, reason="dup", actor_user_id=42)
    db_session.commit()
    assert none is None
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1
    assert len(_events(emitted, "order.paid", "order_id", order.id)) == 1


def test_admin_mark_paid_api_creates_payment(client, db_session, test_admin_user, emitted):
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 1500)
    token = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"}).json()["token"]
    resp = client.post(
        f"/api/admin/commerce/invoices/{invoice.id}/mark-paid",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "cheque"},
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    payments = db_session.execute(select(Payment).where(Payment.invoice_id == invoice.id)).scalars().all()
    assert [(p.gateway, p.amount_cents, p.status) for p in payments] == [("manual", 1500, PaymentStatus.SUCCEEDED)]
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1


def _reseller(db: Session, credit_cents: int) -> Reseller:
    tag = uuid.uuid4().hex[:8]
    reseller = Reseller(user=User(username=f"a0-rsl-{tag}", email=f"a0-rsl-{tag}@example.com", is_reseller=True))
    db.add(reseller)
    db.flush()
    CreditLedgerService.credit(db, reseller.id, credit_cents, description="seed", idempotency_key=f"a0-seed-{tag}")
    db.commit()
    return reseller


def test_reseller_account_invoice_from_credit_publishes_one_invoice_paid(db_session, emitted):
    reseller = _reseller(db_session, 5000)
    account = BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    invoice = InvoiceService.create(
        db_session,
        reseller_id=reseller.id,
        billing_account_id=account.id,
        purpose=InvoicePurpose.CYCLE_CHARGE,
        amount_cents=2000,
        description="reseller account invoice",
    )
    db_session.commit()
    result = PaymentOrchestrator().fund_invoice(db_session, reseller, invoice)
    db_session.commit()
    assert result.funded
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1


def test_reseller_deploy_invoice_from_credit_publishes_nothing(db_session, emitted):
    reseller = _reseller(db_session, 5000)
    invoice = InvoiceService.create(
        db_session,
        reseller_id=reseller.id,
        purpose=InvoicePurpose.DEPLOY_CHARGE,
        amount_cents=2000,
        description="deploy",
    )
    db_session.commit()
    PaymentOrchestrator().fund_invoice(db_session, reseller, invoice)
    db_session.commit()
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
    assert _events(emitted, "invoice.paid", "invoice_id", invoice.id) == []


def test_zero_total_checkout_accept_emits_order_paid_only(db_session, provisioned, emitted):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=0, pricing_model=PricePlanPricingModel.FREE)
    order = _place(db_session, account, user, product, plan, cycle=None)
    assert order.invoice_id is None
    assert order.status == OrderStatus.PENDING_ACCEPTANCE
    CheckoutService.mark_paid_and_fulfill(db_session, order)
    db_session.commit()
    assert db_session.get(Order, order.id).status == OrderStatus.ACTIVE
    assert len(_events(emitted, "order.paid", "order_id", order.id)) == 1
    assert [p for name, p in emitted if name == "invoice.paid"] == []


# Item 4: invoice lines sum to the invoice total.


@pytest.mark.parametrize(
    "setup,price,delta,with_tax,with_coupon",
    [
        (0, 1000, 0, False, False),
        (500, 1000, 0, False, False),
        (500, 1000, 250, False, False),
        (500, 1000, 250, True, False),
        (500, 1000, 250, True, True),
        (333, 777, 111, True, True),
    ],
)
def test_order_invoice_lines_sum_to_total(db_session, setup, price, delta, with_tax, with_coupon):
    user, account = _account(db_session)
    product, plan = _product(db_session, setup_cents=setup, price_cents=price, option_delta=delta)
    country = f"Z{uuid.uuid4().hex[0].upper()}"
    if with_tax:
        BillingProfileDAO.upsert(db_session, account.id, country=country)
        db_session.add(TaxRate(country=country, rate_bps=2000, name="VAT", enabled=True))
    code = None
    if with_coupon:
        code = f"A0{uuid.uuid4().hex[:6]}".upper()
        db_session.add(Coupon(code=code, percent_off=15, applies_to=CouponAppliesTo.ALL,
                              duration=CouponDuration.ONCE, enabled=True))
    db_session.commit()
    order = _place(db_session, account, user, product, plan, options={"ram": "big"} if delta else None,
                   coupon_code=code)
    invoice = db_session.get(Invoice, order.invoice_id)
    lines = db_session.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.sort_order)
    ).scalars().all()
    assert sum(line.total_cents for line in lines) == invoice.amount_cents == order.total_cents
    item = order.items[0]
    # Options are a per-period price: not added to the setup fee.
    assert item.setup_cents == setup
    assert item.recurring_cents == price + delta
    if setup:
        assert any(line.description.endswith("setup fee") and line.total_cents == setup for line in lines)
    if with_tax:
        assert order.tax_cents > 0
        assert sum(line.tax_cents for line in lines) == order.tax_cents


# Item 8: order cancel and provisioning failure.


def test_cancel_unpaid_order_voids_invoice(db_session):
    user, account = _account(db_session)
    product, plan = _product(db_session)
    order = _place(db_session, account, user, product, plan)
    order, decision = cancel_order(db_session, order, actor_user_id=None)
    db_session.commit()
    assert order.status == OrderStatus.CANCELLED
    assert decision is None
    assert db_session.get(Invoice, order.invoice_id).status == InvoiceStatus.VOID


def test_provisioning_failure_records_refund_review(db_session, provisioning_fails):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=900)
    order = _place(db_session, account, user, product, plan)
    record_manual_payment(db_session, order.invoice_id, reason="paid", actor_user_id=None)
    db_session.commit()
    order = db_session.get(Order, order.id)
    assert order.status == OrderStatus.PROVISION_ERROR
    assert "Refund path stub" not in (order.admin_notes or "")
    assert "provisioning_failed" in order.admin_notes
    assert db_session.get(Invoice, order.invoice_id).status == InvoiceStatus.PAID


@pytest.mark.parametrize("policy", ["manual_review", "original_method", "credit"])
def test_cancel_paid_order_records_review_without_moving_money(db_session, provisioning_fails, monkeypatch, policy):
    monkeypatch.setattr(settings, "billing_order_refund_policy", policy)
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=900)
    order = _place(db_session, account, user, product, plan)
    record_manual_payment(db_session, order.invoice_id, reason="paid", actor_user_id=None)
    db_session.commit()
    order, decision = cancel_order(db_session, db_session.get(Order, order.id), actor_user_id=7)
    db_session.commit()
    assert order.status == OrderStatus.CANCELLED
    assert (decision.policy, decision.handling, decision.amount_cents) == (policy, "manual_review", 900)
    invoice = db_session.get(Invoice, order.invoice_id)
    assert invoice.status == InvoiceStatus.PAID
    payments = db_session.execute(select(Payment).where(Payment.invoice_id == invoice.id)).scalars().all()
    assert [p.status for p in payments] == [PaymentStatus.SUCCEEDED]
    # The provisioning failure already recorded this money; cancel does not add a second note.
    assert order.admin_notes.count("Refund review required") == 1


def test_payment_after_cancel_is_flagged_not_fulfilled(db_session, provisioned, emitted):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=900)
    order = _place(db_session, account, user, product, plan)
    invoice = db_session.get(Invoice, order.invoice_id)
    gateway = _FakeCardGateway()
    started = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    db_session.commit()
    cancel_order(db_session, db_session.get(Order, order.id))
    db_session.commit()
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, 900))
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.VOID
    assert db_session.get(Order, order.id).status == OrderStatus.CANCELLED
    payment = db_session.get(Payment, started.payment_id)
    assert payment.status == PaymentStatus.SUCCEEDED
    assert "payment_on_void_invoice" in payment.payment_metadata["refund_review"]
    assert provisioned == []
    assert _events(emitted, "invoice.paid", "invoice_id", invoice.id) == []


def test_admin_cancel_api_reports_refund(client, db_session, test_admin_user, provisioning_fails):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=400)
    order = _place(db_session, account, user, product, plan)
    record_manual_payment(db_session, order.invoice_id, reason="paid", actor_user_id=None)
    db_session.commit()
    token = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"}).json()["token"]
    resp = client.post(f"/api/admin/commerce/orders/{order.id}/cancel", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == OrderStatus.CANCELLED.value
    assert body["refund"] == {"policy": "manual_review", "handling": "manual_review", "amount_cents": 400,
                              "currency": "USD"}


# Item 9: coupons.


def _coupon(db: Session, **kwargs) -> Coupon:
    values = dict(code=f"C{uuid.uuid4().hex[:8]}".upper(), amount_off_cents=100, applies_to=CouponAppliesTo.ALL,
                  duration=CouponDuration.ONCE, enabled=True)
    values.update(kwargs)
    coupon = Coupon(**values)
    db.add(coupon)
    db.commit()
    return coupon


def _redemptions(db: Session, coupon: Coupon) -> int:
    return db.scalar(select(func.count(CouponRedemption.id)).where(CouponRedemption.coupon_id == coupon.id))


def test_coupon_usage_counted_at_payment_not_placement(db_session, provisioned):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    coupon = _coupon(db_session)
    order = _place(db_session, account, user, product, plan, coupon_code=coupon.code)
    db_session.refresh(coupon)
    assert (coupon.used_count, _redemptions(db_session, coupon)) == (0, 0)
    record_manual_payment(db_session, order.invoice_id, reason="paid", actor_user_id=None)
    db_session.commit()
    db_session.refresh(coupon)
    assert coupon.used_count == 1
    redemption = db_session.execute(select(CouponRedemption).where(CouponRedemption.coupon_id == coupon.id)).scalar_one()
    assert (redemption.order_id, redemption.invoice_id) == (order.id, order.invoice_id)
    record_coupon_usage(db_session, order=db_session.get(Order, order.id))
    db_session.commit()
    db_session.refresh(coupon)
    assert (coupon.used_count, _redemptions(db_session, coupon)) == (1, 1)


def test_coupon_max_uses_per_account_enforced(db_session, provisioned):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    coupon = _coupon(db_session, max_uses_per_account=1)
    order = _place(db_session, account, user, product, plan, coupon_code=coupon.code)
    record_manual_payment(db_session, order.invoice_id, reason="paid", actor_user_id=None)
    db_session.commit()
    with pytest.raises(CheckoutError, match="usage limit"):
        CheckoutService.quote(db_session, account, product.id, plan.id, "monthly", coupon_code=coupon.code)
    _, other = _account(db_session)
    quote = CheckoutService.quote(db_session, other, product.id, plan.id, "monthly", coupon_code=coupon.code)
    assert quote.discount_cents == 100


def test_unpaid_order_does_not_consume_per_account_limit(db_session):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    coupon = _coupon(db_session, max_uses_per_account=1)
    _place(db_session, account, user, product, plan, coupon_code=coupon.code)
    quote = CheckoutService.quote(db_session, account, product.id, plan.id, "monthly", coupon_code=coupon.code)
    assert quote.discount_cents == 100


def test_repeating_coupon_requires_duration_months(db_session):
    _, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    broken = _coupon(db_session, duration=CouponDuration.REPEATING, duration_months=None)
    with pytest.raises(CheckoutError, match="not configured"):
        CheckoutService.quote(db_session, account, product.id, plan.id, "monthly", coupon_code=broken.code)
    ok = _coupon(db_session, duration=CouponDuration.REPEATING, duration_months=3)
    quote = CheckoutService.quote(db_session, account, product.id, plan.id, "monthly", coupon_code=ok.code)
    assert quote.discount_cents == 100


# The generic commerce settings endpoint must not write settings-store rows.


def test_generic_settings_endpoint_rejects_settings_store_keys(client, db_session, test_admin_user):
    from app.services import settings_store

    token = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    key = f"{settings_store.STORAGE_PREFIX}billing_order_refund_policy"
    put = client.put(f"/api/admin/commerce/settings/{key}", headers=headers, json={"value": {"v": "credit"}})
    assert put.status_code == 400
    assert "/api/admin/settings" in put.json()["detail"]
    # MySQL compares primary keys case-insensitively, so a differently cased prefix must be refused too.
    upper = client.put(f"/api/admin/commerce/settings/ {key.upper()}", headers=headers, json={"value": {"v": "x"}})
    assert upper.status_code == 400
    ok = client.put("/api/admin/commerce/settings/a0_plain_key", headers=headers, json={"value": {"v": "x"}})
    assert ok.status_code == 200, ok.text


# Billing integrity detectors (E2) stay quiet for invoices settled through the A0 paths.


_STRICT = {
    "min_age_minutes": 0,
    "settled_grace_minutes": 0,
    "pending_after_minutes": 0,
}


def _integrity_findings(db: Session, invoice: Invoice) -> dict[str, list]:
    from datetime import datetime, timedelta

    from app.services.alerting import detectors
    from app.services.alerting.catalogue import CATALOGUE

    later = datetime.utcnow() + timedelta(minutes=1)
    payment_ids = {
        str(p.id) for p in db.execute(select(Payment).where(Payment.invoice_id == invoice.id)).scalars()
    }
    found = {}
    for name in ("billing_paid_without_payment", "billing_orphaned_payment", "billing_invoice_lines_mismatch",
                 "billing_overallocated"):
        params = {**CATALOGUE[name].defaults(), **_STRICT}
        params = {key: value for key, value in params.items() if key in CATALOGUE[name].defaults()}
        findings = detectors.DETECTORS[name](db, params, later)
        found[name] = [
            f for f in findings
            if (f.resource_type == "invoice" and f.resource_id == str(invoice.id))
            or (f.resource_type == "payment" and f.resource_id in payment_ids)
        ]
    return found


def test_card_payment_settled_by_webhook_leaves_integrity_detectors_quiet(db_session, provisioned):
    user, account = _account(db_session)
    product, plan = _product(db_session, setup_cents=500, price_cents=1000, option_delta=250)
    order = _place(db_session, account, user, product, plan, options={"ram": "big"})
    invoice = db_session.get(Invoice, order.invoice_id)
    gateway = _FakeCardGateway()
    started = start_card_payment(db_session, invoice, billing_account_id=account.id, registry=_registry(gateway))
    db_session.commit()
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, invoice.amount_cents))
    invoice = db_session.get(Invoice, invoice.id)
    assert invoice.status == InvoiceStatus.PAID
    assert _integrity_findings(db_session, invoice) == {
        "billing_paid_without_payment": [],
        "billing_orphaned_payment": [],
        "billing_invoice_lines_mismatch": [],
        "billing_overallocated": [],
    }


def test_admin_mark_paid_leaves_integrity_detectors_quiet(db_session, provisioned):
    user, account = _account(db_session)
    product, plan = _product(db_session, setup_cents=300, price_cents=1200)
    order = _place(db_session, account, user, product, plan)
    record_manual_payment(db_session, order.invoice_id, reason="wire received", actor_user_id=1)
    db_session.commit()
    invoice = db_session.get(Invoice, order.invoice_id)
    assert invoice.status == InvoiceStatus.PAID
    assert _integrity_findings(db_session, invoice) == {
        "billing_paid_without_payment": [],
        "billing_orphaned_payment": [],
        "billing_invoice_lines_mismatch": [],
        "billing_overallocated": [],
    }


def test_detectors_flag_the_old_admin_mark_paid_shape(db_session):
    """A paid invoice with no payment (what admin mark-paid used to leave) is what the detector reports."""
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 800)
    invoice.status = InvoiceStatus.PAID
    db_session.commit()
    findings = _integrity_findings(db_session, invoice)
    assert [f.alert_type for f in findings["billing_paid_without_payment"]] == ["billing_paid_without_payment"]


def test_open_card_attempt_on_invoice_paid_by_admin_is_reported_as_orphaned(db_session):
    """The client may still confirm that PaymentIntent, so it is left for a person to cancel, and reported."""
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 800)
    started = start_card_payment(
        db_session, invoice, billing_account_id=account.id, registry=_registry(_FakeCardGateway())
    )
    db_session.commit()
    record_manual_payment(db_session, invoice.id, reason="wire received", actor_user_id=1)
    db_session.commit()
    findings = _integrity_findings(db_session, db_session.get(Invoice, invoice.id))
    assert findings["billing_paid_without_payment"] == []
    assert [f.resource_id for f in findings["billing_orphaned_payment"]] == [str(started.payment_id)]
