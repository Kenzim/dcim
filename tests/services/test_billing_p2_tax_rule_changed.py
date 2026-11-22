"""A rule closed or superseded before the tax point refuses the publish (design 4.9, 15.5).

On MariaDB the publish reads its rules from the transaction's snapshot, so a
supersede committed meanwhile is unseen by the engine; the locking read in
``invoice_tax.lock_rules`` sees it. Here the supersede is written right after
the engine has run, which is what that snapshot looks like on SQLite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.models.commerce_invoice_ext import InvoiceLineKind
from app.models.commerce_order import Order
from app.models.reseller import BillingCycle, Invoice, InvoicePurpose, ServiceBillingStatus
from app.models.tax import InvoiceLineTax, TaxRule
from app.services.checkout_service import CheckoutError
from app.services.invoice_service import InvoiceService, NewLine
from app.services.tax import invoice_tax
from app.services.tax.invoice_tax import TAX_RULES_CHANGED_MESSAGE, TaxRuleChanged
from tests.services.tax_helpers import (
    add_registration,
    add_rule,
    enable_tax,
    invoice_v2,
    retail_account,
    seed_classes,
    set_engine,
    stub_legal_notes,
)


@pytest.fixture
def gb_account(db_session, monkeypatch):
    stub_legal_notes(monkeypatch)
    seed_classes(db_session)
    _user, account = retail_account(db_session, country="GB")
    return account


@pytest.fixture
def rule(db_session):
    invoice_v2(db_session)
    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB")
    return add_rule(db_session, name="UK VAT 20%", jurisdiction="GB", source="pack")


def _now():
    return datetime.now(timezone.utc)


def _successor(db, old, *, at, rate="25"):
    values = {
        c.name: getattr(old, c.name)
        for c in TaxRule.__table__.columns
        if c.name not in ("id", "created_at", "updated_at", "valid_to", "legacy_tax_rate_id")
    }
    new = TaxRule(**{**values, "rate_percent": rate, "valid_from": at, "supersedes_id": old.id, "name": "UK VAT 25%"})
    db.add(new)
    db.flush()
    return new


def supersede_after_calculation(monkeypatch, db, rule_id, *, close=True, at=None):
    """After the engine has run, supersede ``rule_id`` as another transaction would."""
    real = invoice_tax.calculate_verified

    def calculate(*args, **kwargs):
        result = real(*args, **kwargs)
        when = at or _now() - timedelta(seconds=1)
        if close:
            db.execute(update(TaxRule).where(TaxRule.id == rule_id).values(valid_to=when))
        _successor(db, db.get(TaxRule, rule_id), at=when)
        return result

    monkeypatch.setattr(invoice_tax, "calculate_verified", calculate)


def _draft(db, account):
    return InvoiceService.create(
        db, billing_account_id=account.id, purpose=InvoicePurpose.CYCLE_CHARGE, amount_cents=1000,
        currency="GBP", publish=False,
        lines=[NewLine(description="Server", unit_cents=1000, kind=InvoiceLineKind.PRODUCT)],
    )


def _tax_rows(db, invoice_id):
    return list(db.execute(select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice_id)).scalars())


def test_publish_refuses_a_rule_closed_before_the_tax_point(db_session, gb_account, rule, monkeypatch):
    draft = _draft(db_session, gb_account)
    supersede_after_calculation(monkeypatch, db_session, rule.id)
    with pytest.raises(TaxRuleChanged) as raised:
        InvoiceService.publish(db_session, draft)
    assert raised.value.rule_ids == (rule.id,)
    db_session.rollback()


def test_publish_refuses_a_rule_with_a_successor_in_force(db_session, gb_account, rule, monkeypatch):
    draft = _draft(db_session, gb_account)
    supersede_after_calculation(monkeypatch, db_session, rule.id, close=False)
    with pytest.raises(TaxRuleChanged):
        InvoiceService.publish(db_session, draft)
    db_session.rollback()


def test_create_refuses_a_rule_closed_before_its_tax_point(db_session, gb_account, rule, monkeypatch):
    supersede_after_calculation(monkeypatch, db_session, rule.id)
    with pytest.raises(TaxRuleChanged):
        _draft(db_session, gb_account)
    db_session.rollback()


def test_a_supersede_from_a_later_date_keeps_the_rule(db_session, gb_account, rule, monkeypatch):
    draft = _draft(db_session, gb_account)
    supersede_after_calculation(monkeypatch, db_session, rule.id, at=_now() + timedelta(days=1))
    published = InvoiceService.publish(db_session, draft)
    assert [(r.tax_rule_id, r.tax_cents) for r in _tax_rows(db_session, published.id)] == [(rule.id, 200)]


def test_checkout_asks_the_customer_to_try_again(db_session, rule, monkeypatch):
    from tests.services.test_billing_a0_money_path import _account, _product
    from app.dao.commerce_dao import BillingProfileDAO

    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    stub_legal_notes(monkeypatch)
    user, account = _account(db_session, "p2t-rule-changed")
    BillingProfileDAO.upsert(db_session, account.id, country="GB")
    account.currency = "GBP"
    product, plan = _product(db_session, price_cents=1000)
    plan.currency = "GBP"
    db_session.flush()
    supersede_after_calculation(monkeypatch, db_session, rule.id)
    from app.services.checkout_service import CheckoutService

    with pytest.raises(CheckoutError, match="please try again") as raised:
        CheckoutService.place_order(
            db_session, account=account, user_id=user.id, frontend_product_id=product.id,
            price_plan_id=plan.id, cycle_interval="monthly", checkout_nonce="rule-changed", terms_version="1",
        )
    assert str(raised.value) == TAX_RULES_CHANGED_MESSAGE
    db_session.rollback()
    assert db_session.scalar(select(Order.id).where(Order.checkout_nonce == "rule-changed")) is None


@pytest.mark.asyncio
async def test_recurring_worker_leaves_the_billing_for_its_next_pass(db_session, monkeypatch):
    from tests.services.test_recurring_billing_service import (
        FakeGateway,
        FakeLifecycle,
        MutableClock,
        _billing,
        _config,
        _reseller,
        _service,
    )

    due = datetime(2027, 5, 10, 9, tzinfo=timezone.utc)
    reseller = _reseller(db_session, "rule-changed")
    first = _billing(db_session, reseller, "rule-changed-a", due - timedelta(hours=1))
    second = _billing(db_session, reseller, "rule-changed-b", due)
    first_id, second_id = first.id, second.id
    real_create = InvoiceService.create_cycle
    raised = []

    def create_cycle(db, **kwargs):
        if kwargs["service_id"] == first.service_id and not raised:
            raised.append(True)
            raise TaxRuleChanged([1])
        return real_create(db, **kwargs)

    monkeypatch.setattr(InvoiceService, "create_cycle", staticmethod(create_cycle))
    worker = _service(_config(), FakeGateway(["success"] * 3), FakeLifecycle(), MutableClock(due))

    result = await worker.process_batch(db_session, owner="worker-a")
    assert raised and result["paid"] == 1

    def cycles(billing_id):
        return db_session.scalars(select(BillingCycle).where(BillingCycle.service_billing_id == billing_id)).all()

    assert cycles(first_id) == [] and len(cycles(second_id)) == 1
    db_session.refresh(first)
    assert first.status == ServiceBillingStatus.ACTIVE and first.last_failure_code is None
    assert first.next_charge_at.replace(tzinfo=timezone.utc) == due - timedelta(hours=1)

    again = await worker.process_batch(db_session, owner="worker-a")
    assert again["paid"] == 1
    (cycle,) = cycles(first_id)
    assert cycle.invoice_id is not None and db_session.get(Invoice, cycle.invoice_id) is not None
    assert len(cycles(second_id)) == 1
