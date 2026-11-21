"""Billing integrity detectors (spec E2): each finds the broken record and ignores the healthy ones."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, String, Table

from app.models.commerce_invoice_ext import InvoiceLine
from app.models.reseller import (
    CreditLedgerEntry,
    CreditLedgerEntryType,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
)
from app.models.user import User
from app.services.alerting import billing_integrity as bi
from app.services.alerting import detectors as d
from app.services.alerting.catalogue import CATALOGUE, effective_settings, update_type_setting

NOW = datetime.utcnow()
BILLING_TYPES = (
    "billing_invoice_lines_mismatch",
    "billing_paid_without_payment",
    "billing_overallocated",
    "billing_credit_drift",
    "billing_orphaned_payment",
    "billing_refund_stuck",
)
_numbers = iter(range(9000, 100000))


def run(db, name, **overrides):
    params = {**CATALOGUE[name].defaults(), **overrides}
    return d.DETECTORS[name](db, params, NOW)


def ago(**kw):
    return NOW - timedelta(**kw)


def make_invoice(db, amount=1000, status=InvoiceStatus.OPEN, age=timedelta(days=2), lines=()):
    invoice = Invoice(
        invoice_number=next(_numbers),
        purpose=InvoicePurpose.ORDER_CHARGE,
        status=status,
        amount_cents=amount,
        currency="USD",
        created_at=NOW - age,
    )
    db.add(invoice)
    db.flush()
    for order, total in enumerate(lines):
        db.add(InvoiceLine(invoice_id=invoice.id, description=f"line {order}", unit_cents=total, total_cents=total,
                           sort_order=order))
    db.commit()
    return invoice


def make_payment(db, invoice, amount=1000, status=PaymentStatus.SUCCEEDED, external_ref=None, age=timedelta(days=2),
                 gateway="stripe"):
    payment = Payment(invoice_id=invoice.id, gateway=gateway, status=status, amount_cents=amount, currency="USD",
                      external_ref=external_ref, created_at=NOW - age)
    db.add(payment)
    db.commit()
    return payment


def make_reseller(db, name, cached=0):
    reseller = Reseller(user=User(username=f"{name}-r", email=f"{name}@example.com"), cached_balance_cents=cached)
    db.add(reseller)
    db.commit()
    return reseller


def add_entry(db, reseller, amount, balance_after, entry_type=CreditLedgerEntryType.TOPUP):
    entry = CreditLedgerEntry(reseller_id=reseller.id, entry_type=entry_type, amount_cents=amount,
                              balance_after_cents=balance_after)
    db.add(entry)
    db.commit()
    return entry


def ids(findings):
    return sorted(f.resource_id for f in findings)


# ------------------------------------------------------------------ registration and defaults


def test_every_billing_type_is_in_the_catalogue_and_detectors():
    for key in BILLING_TYPES:
        assert key in CATALOGUE and key in d.DETECTORS
        assert CATALOGUE[key].audience == "staff"
        assert set(CATALOGUE[key].defaults()) >= {"max_rows"}


def test_default_enabled_state(db_session):
    states = {key: setting.enabled for key, setting in effective_settings(db_session).items() if key in BILLING_TYPES}
    assert states == {
        "billing_invoice_lines_mismatch": False,
        "billing_paid_without_payment": False,
        "billing_overallocated": False,
        "billing_credit_drift": False,
        "billing_orphaned_payment": False,
        "billing_refund_stuck": True,
    }


def test_changing_only_a_parameter_keeps_the_default_state(db_session):
    update_type_setting(db_session, "billing_orphaned_payment", params={"pending_after_minutes": 120})
    update_type_setting(db_session, "billing_overallocated", params={"max_rows": 5})
    settings = effective_settings(db_session)
    assert settings["billing_orphaned_payment"].enabled is False
    assert settings["billing_orphaned_payment"].params["pending_after_minutes"] == 120
    assert settings["billing_overallocated"].enabled is False
    assert settings["billing_refund_stuck"].enabled is True
    update_type_setting(db_session, "billing_orphaned_payment", enabled=True)
    assert effective_settings(db_session)["billing_orphaned_payment"].enabled is True
    update_type_setting(db_session, "billing_orphaned_payment", reset=True)
    assert effective_settings(db_session)["billing_orphaned_payment"].enabled is False


def test_pre_existing_types_stay_enabled_by_default(db_session):
    settings = effective_settings(db_session)
    assert all(s.enabled for key, s in settings.items() if key not in BILLING_TYPES)


# ------------------------------------------------------------------ invoice lines


def test_lines_mismatch_found_with_ids(db_session):
    bad = make_invoice(db_session, 1000, lines=(600, 300))
    findings = run(db_session, "billing_invoice_lines_mismatch")
    assert ids(findings) == [str(bad.id)]
    details = findings[0].details
    assert details["amount_cents"] == 1000 and details["lines_total_cents"] == 900
    assert details["difference_cents"] == -100 and details["invoice_id"] == bad.id


def test_lines_mismatch_ignores_healthy_shapes(db_session):
    make_invoice(db_session, 1000, lines=(700, 300))
    make_invoice(db_session, 1000, lines=(1200, -300, 100))
    make_invoice(db_session, 1000)
    make_invoice(db_session, 1000, status=InvoiceStatus.VOID, lines=(1,))
    make_invoice(db_session, 1000, status=InvoiceStatus.DRAFT, lines=(1,))
    make_invoice(db_session, 1000, age=timedelta(minutes=5), lines=(1,))
    make_invoice(db_session, 1000, age=timedelta(days=400), lines=(1,))
    assert run(db_session, "billing_invoice_lines_mismatch") == []


def test_lines_mismatch_threshold_parameters(db_session):
    young = make_invoice(db_session, 1000, age=timedelta(minutes=30), lines=(1,))
    assert run(db_session, "billing_invoice_lines_mismatch") == []
    assert ids(run(db_session, "billing_invoice_lines_mismatch", min_age_minutes=10)) == [str(young.id)]


# ------------------------------------------------------------------ paid invoices have payments


def test_paid_without_payment_found(db_session):
    none = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    short = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    make_payment(db_session, short, 400)
    make_payment(db_session, short, 500, status=PaymentStatus.PENDING)
    findings = run(db_session, "billing_paid_without_payment")
    assert ids(findings) == sorted([str(none.id), str(short.id)])
    by_id = {f.details["invoice_id"]: f.details for f in findings}
    assert by_id[none.id]["succeeded_payments_cents"] == 0 and by_id[none.id]["shortfall_cents"] == 1000
    assert by_id[short.id]["succeeded_payments_cents"] == 400 and by_id[short.id]["shortfall_cents"] == 600


def test_paid_with_a_refunded_payment_is_reported(db_session):
    invoice = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    make_payment(db_session, invoice, 1000, status=PaymentStatus.REFUNDED)
    assert ids(run(db_session, "billing_paid_without_payment")) == [str(invoice.id)]


def test_paid_without_payment_ignores_covered_and_unpaid(db_session):
    exact = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    make_payment(db_session, exact, 1000)
    split = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    make_payment(db_session, split, 600, gateway="credit")
    make_payment(db_session, split, 400)
    make_invoice(db_session, 1000, status=InvoiceStatus.OPEN)
    make_invoice(db_session, 1000, status=InvoiceStatus.VOID)
    make_invoice(db_session, 1000, status=InvoiceStatus.PAID, age=timedelta(minutes=5))
    assert run(db_session, "billing_paid_without_payment") == []


# ------------------------------------------------------------------ overallocation


def test_overallocated_found(db_session):
    over = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    make_payment(db_session, over, 700)
    make_payment(db_session, over, 500)
    findings = run(db_session, "billing_overallocated")
    assert ids(findings) == [str(over.id)]
    assert findings[0].details["excess_cents"] == 200 and findings[0].details["amount_cents"] == 1000


def test_overallocated_ignores_exact_partial_refunded_and_pending(db_session):
    exact = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    make_payment(db_session, exact, 1000)
    partial = make_invoice(db_session, 1000)
    make_payment(db_session, partial, 400)
    refunded = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    make_payment(db_session, refunded, 1000, status=PaymentStatus.REFUNDED)
    make_payment(db_session, refunded, 1000)
    pending = make_invoice(db_session, 1000)
    make_payment(db_session, pending, 1000)
    make_payment(db_session, pending, 5000, status=PaymentStatus.PENDING)
    make_payment(db_session, pending, 5000, status=PaymentStatus.FAILED)
    assert run(db_session, "billing_overallocated") == []


# ------------------------------------------------------------------ credit ledger


def test_credit_drift_found_for_sum_and_for_latest_balance(db_session):
    by_sum = make_reseller(db_session, "bysum", cached=900)
    add_entry(db_session, by_sum, 1000, 1000)
    by_latest = make_reseller(db_session, "bylatest", cached=700)
    add_entry(db_session, by_latest, 1000, 1000)
    add_entry(db_session, by_latest, -300, 600, CreditLedgerEntryType.CHARGE)
    findings = run(db_session, "billing_credit_drift")
    assert ids(findings) == sorted([str(by_sum.id), str(by_latest.id)])
    detail = {f.details["reseller_id"]: f.details for f in findings}
    assert detail[by_sum.id] == {**detail[by_sum.id], "cached_balance_cents": 900, "ledger_sum_cents": 1000,
                                 "latest_balance_after_cents": 1000, "ledger_entries": 1}
    assert detail[by_latest.id]["ledger_sum_cents"] == 700 and detail[by_latest.id]["latest_balance_after_cents"] == 600


def test_credit_drift_ignores_consistent_and_ledgerless_resellers(db_session):
    good = make_reseller(db_session, "good", cached=700)
    add_entry(db_session, good, 1000, 1000)
    add_entry(db_session, good, -300, 700, CreditLedgerEntryType.CHARGE)
    make_reseller(db_session, "empty", cached=0)
    make_reseller(db_session, "legacy", cached=500)
    assert run(db_session, "billing_credit_drift") == []


# ------------------------------------------------------------------ orphaned payments


def test_orphaned_payment_with_gateway_reference(db_session):
    invoice = make_invoice(db_session, 1000)
    stale = make_payment(db_session, invoice, 1000, status=PaymentStatus.PENDING, external_ref="pi_stale",
                         age=timedelta(days=2))
    findings = run(db_session, "billing_orphaned_payment")
    assert ids(findings) == [str(stale.id)]
    assert findings[0].details["reasons"] == ["pending_with_gateway_reference"]
    assert findings[0].details["external_ref"] == "pi_stale" and findings[0].details["invoice_id"] == invoice.id


def test_orphaned_payment_whose_invoice_is_settled(db_session):
    paid = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    void = make_invoice(db_session, 1000, status=InvoiceStatus.VOID)
    a = make_payment(db_session, paid, 1000, status=PaymentStatus.PENDING, age=timedelta(hours=3))
    b = make_payment(db_session, void, 1000, status=PaymentStatus.PENDING, external_ref="pi_void",
                     age=timedelta(hours=3))
    findings = {f.resource_id: f for f in run(db_session, "billing_orphaned_payment")}
    assert sorted(findings) == sorted([str(a.id), str(b.id)])
    assert findings[str(a.id)].details["reasons"] == ["invoice_paid"]
    assert findings[str(b.id)].details["reasons"] == ["invoice_void"]


def test_orphaned_payment_ignores_recent_resolved_and_reference_less(db_session):
    open_invoice = make_invoice(db_session, 1000)
    make_payment(db_session, open_invoice, 1000, status=PaymentStatus.PENDING, external_ref="pi_new",
                 age=timedelta(hours=1))
    make_payment(db_session, open_invoice, 1000, status=PaymentStatus.PENDING, age=timedelta(days=9))
    make_payment(db_session, open_invoice, 1000, status=PaymentStatus.FAILED, external_ref="pi_failed",
                 age=timedelta(days=9))
    paid = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    make_payment(db_session, paid, 1000, status=PaymentStatus.PENDING, age=timedelta(minutes=10))
    make_payment(db_session, paid, 1000, status=PaymentStatus.SUCCEEDED, external_ref="pi_ok", age=timedelta(days=9))
    assert run(db_session, "billing_orphaned_payment") == []


def test_orphaned_payment_threshold_parameter(db_session):
    invoice = make_invoice(db_session, 1000)
    payment = make_payment(db_session, invoice, 1000, status=PaymentStatus.PENDING, external_ref="pi_x",
                           age=timedelta(hours=2))
    assert run(db_session, "billing_orphaned_payment") == []
    assert ids(run(db_session, "billing_orphaned_payment", pending_after_minutes=60)) == [str(payment.id)]


# ------------------------------------------------------------------ pending refunds


@pytest.fixture
def refunds_table(db_session):
    db_session.rollback()
    engine = db_session.get_bind()
    table = Table(
        "refunds", MetaData(),
        Column("id", Integer, primary_key=True),
        Column("payment_id", Integer),
        Column("amount_cents", BigInteger),
        Column("status", String(32)),
        Column("created_at", DateTime),
    )
    table.create(engine)
    yield table
    db_session.rollback()
    table.drop(engine)


def test_refund_stuck_is_empty_when_there_is_no_refunds_table(db_session):
    assert run(db_session, "billing_refund_stuck") == []


def test_refund_stuck_found_and_healthy_ignored(db_session, refunds_table):
    db_session.execute(refunds_table.insert(), [
        {"id": 1, "payment_id": 5, "amount_cents": 700, "status": "pending", "created_at": ago(hours=3)},
        {"id": 2, "payment_id": 6, "amount_cents": 100, "status": "pending", "created_at": ago(minutes=5)},
        {"id": 3, "payment_id": 7, "amount_cents": 100, "status": "succeeded", "created_at": ago(days=3)},
        {"id": 4, "payment_id": 8, "amount_cents": 100, "status": "failed", "created_at": ago(days=3)},
    ])
    db_session.commit()
    findings = run(db_session, "billing_refund_stuck")
    assert ids(findings) == ["1"]
    assert findings[0].details["refund_id"] == 1
    assert findings[0].details["payment_id"] == 5 and findings[0].details["amount_cents"] == 700
    assert ids(run(db_session, "billing_refund_stuck", pending_after_minutes=1)) == ["1", "2"]


# ------------------------------------------------------------------ row limit


def test_row_limit_lists_max_rows_and_reports_the_remainder(db_session):
    made = [make_invoice(db_session, 1000, status=InvoiceStatus.PAID) for _ in range(5)]
    findings = run(db_session, "billing_paid_without_payment", max_rows=3)
    listed = [f for f in findings if f.resource_id != bi.OVERFLOW_ID]
    overflow = [f for f in findings if f.resource_id == bi.OVERFLOW_ID]
    assert ids(listed) == sorted(str(i.id) for i in made[:3])
    assert len(overflow) == 1 and overflow[0].details == {"remaining": 2, "shown": 3}
    assert "2 more" in overflow[0].title


def test_no_overflow_finding_when_everything_fits(db_session):
    for _ in range(3):
        make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    assert [f for f in run(db_session, "billing_paid_without_payment", max_rows=3) if f.resource_id == bi.OVERFLOW_ID] == []


@pytest.mark.parametrize("name", ["billing_invoice_lines_mismatch", "billing_overallocated"])
def test_row_limit_for_other_invoice_detectors(db_session, name):
    for _ in range(4):
        invoice = make_invoice(db_session, 1000, lines=(1,))
        make_payment(db_session, invoice, 1500)
    findings = run(db_session, name, max_rows=2)
    assert len(findings) == 3 and findings[-1].resource_id == bi.OVERFLOW_ID
    assert findings[-1].details["remaining"] == 2


def test_credit_drift_and_orphan_row_limits(db_session):
    for n in range(3):
        reseller = make_reseller(db_session, f"drift{n}", cached=1)
        add_entry(db_session, reseller, 5, 5)
    findings = run(db_session, "billing_credit_drift", max_rows=2)
    assert len(findings) == 3 and findings[-1].details["remaining"] == 1
    invoice = make_invoice(db_session, 1000)
    for n in range(3):
        make_payment(db_session, invoice, 10, status=PaymentStatus.PENDING, external_ref=f"pi_{n}",
                     age=timedelta(days=3))
    findings = run(db_session, "billing_orphaned_payment", max_rows=2)
    assert len(findings) == 3 and findings[-1].details["remaining"] == 1


def test_findings_are_json_serialisable(db_session):
    import json

    invoice = make_invoice(db_session, 1000, status=InvoiceStatus.PAID, lines=(1,))
    make_payment(db_session, invoice, 5000, status=PaymentStatus.PENDING, external_ref="pi_j", age=timedelta(days=3))
    for name in BILLING_TYPES:
        for finding in run(db_session, name):
            json.dumps(finding.details)


def test_detectors_do_not_write(db_session):
    make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    before = db_session.query(Invoice).count(), db_session.query(Payment).count()
    for name in BILLING_TYPES:
        run(db_session, name)
    assert not db_session.new and not db_session.dirty and not db_session.deleted
    assert (db_session.query(Invoice).count(), db_session.query(Payment).count()) == before


# ------------------------------------------------------------------ through the engine


def test_engine_opens_alerts_for_enabled_types_and_skips_disabled_ones(db_session):
    from app.models.alerting import Alert
    from app.services.alerting.engine import run_cycle

    over = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    make_payment(db_session, over, 700)
    make_payment(db_session, over, 700)
    invoice = make_invoice(db_session, 1000, status=InvoiceStatus.PAID)
    stale = make_payment(db_session, invoice, 1000, status=PaymentStatus.PENDING, external_ref="pi_e",
                         age=timedelta(days=3))

    run_cycle(db_session, now=NOW, only_types=BILLING_TYPES)
    assert db_session.query(Alert).all() == []

    update_type_setting(db_session, "billing_overallocated", enabled=True)
    db_session.commit()
    run_cycle(db_session, now=NOW, only_types=BILLING_TYPES)
    alerts = db_session.query(Alert).all()
    assert [(a.alert_type, a.resource_id) for a in alerts] == [("billing_overallocated", str(over.id))]
    assert alerts[0].severity == "critical" and alerts[0].details["excess_cents"] == 400
    assert stale.id

    update_type_setting(db_session, "billing_paid_without_payment", enabled=True)
    db_session.commit()
    run_cycle(db_session, now=NOW, only_types=BILLING_TYPES)
    types = sorted(a.alert_type for a in db_session.query(Alert).all())
    assert types == ["billing_overallocated", "billing_paid_without_payment"]
