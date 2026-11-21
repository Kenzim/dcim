"""Billing phase 1 foundations (P1-S): event registry, numbering, balance, shadow, periodic jobs."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.database import Base
from app.models.billing_series import DocumentSeries, DocumentType, series_counter_name
from app.models.billing_shadow import BillingShadowMismatch
from app.models.commerce_account import BillingAccount, SystemSetting
from app.models.invoice_adjustment import (
    AdjustmentKind,
    AdjustmentOutcome,
    AdjustmentReason,
    InvoiceAdjustment,
)
from app.models.reseller import (
    Invoice,
    InvoicePurpose,
    InvoiceSequence,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.plugin_host import catalogue
from app.services.billing import periodic, settlement, settlement_subscribers
from app.services.billing.cycles import CycleError, resolve_cycle
from app.services.billing_shadow import BillingShadowService, billing_engine_mode
from app.services.invoice_numbering import InvoiceNumbering, NumberingError
from app.services.invoice_service import InvoiceService
from app.services.settings_store import get_setting, set_setting

NOW = datetime(2027, 3, 4, 3, 15, tzinfo=timezone.utc)
NEW_EVENTS = (
    "invoice.created",
    "invoice.overdue",
    "invoice.voided",
    "invoice.refunded",
    "credit_note.issued",
    "debit_note.issued",
    "payment.refunded",
    "credit.added",
    "credit.applied",
)


@pytest.fixture
def file_db(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'p1.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    yield factory
    engine.dispose()


def _invoice(db, number=5001, amount=1000, **values):
    invoice = Invoice(
        invoice_number=number,
        purpose=InvoicePurpose.DEPLOY_CHARGE,
        status=InvoiceStatus.OPEN,
        amount_cents=amount,
        currency="USD",
        **values,
    )
    db.add(invoice)
    db.flush()
    return invoice


def _series(db, code="CN", **values):
    fields = {
        "code": code,
        "document_type": DocumentType.CREDIT_NOTE,
        "name": code,
        "format": "{SERIES}-{NUMBER}",
        "number_source": "own",
        "gapless": False,
        "reset_period": "never",
        **values,
    }
    series = DocumentSeries(**fields)
    db.add(series)
    db.flush()
    return series


# ------------------------------------------------------------------ event registry


@pytest.fixture
def clean_event(request):
    names = []

    def register(event, name, handler, **kwargs):
        settlement.subscribe(event, name, handler, **kwargs)
        names.append((event, name))

    yield register
    for event, name in names:
        settlement.unsubscribe(event, name)


def test_invoice_paid_subscribers_keep_order_and_names():
    assert settlement.invoice_paid_subscribers() == [
        "coupon_usage",
        "complete_billing_cycle",
        "publish_invoice_paid",
        "invoice_paid_email",
        "fulfil_order",
    ]
    assert settlement.subscribers("invoice.paid") == settlement.invoice_paid_subscribers()


def test_every_new_catalogue_event_has_exactly_one_publisher():
    for event in NEW_EVENTS:
        assert settlement.subscribers(event) == [f"catalogue:{event}"]
        spec = catalogue.EVENTS[event]
        assert spec.permission == "billing.read"


def test_invoice_paid_has_one_emit_call_in_the_app():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app"
    pattern = re.compile(r"emit\(\s*\w+\s*,\s*[\"']invoice\.paid[\"']")
    hits = [(path.name, len(pattern.findall(path.read_text()))) for path in root.rglob("*.py")]
    assert [hit for hit in hits if hit[1]] == [("settlement_subscribers.py", 1)]


def test_fire_runs_in_order_isolates_failures_and_honours_applies(db_session, clean_event, caplog):
    calls = []

    def boom(db, payload):
        calls.append("boom")
        raise RuntimeError("side effect failed")

    clean_event("test.event", "first", lambda db, p: calls.append(("first", p)))
    clean_event("test.event", "boom", boom)
    clean_event("test.event", "skipped", lambda db, p: calls.append("skipped"), applies=lambda p: p != "x")
    clean_event("test.event", "last", lambda db, p: calls.append("last"))
    with caplog.at_level(logging.ERROR):
        settlement.fire(db_session, "test.event", "x")
    assert calls == [("first", "x"), "boom", "last"]
    assert "Billing event test.event subscriber boom failed" in caplog.text


def test_subscribe_replaces_by_name(clean_event):
    clean_event("test.replace", "one", lambda db, p: None)
    clean_event("test.replace", "two", lambda db, p: None)
    clean_event("test.replace", "one", lambda db, p: None)
    assert settlement.subscribers("test.replace") == ["one", "two"]


def test_subscribe_invoice_paid_applies_still_receives_the_invoice(db_session):
    seen = []
    settlement.subscribe_invoice_paid(
        "p1s_probe", lambda db, event: seen.append(event.invoice.id), applies=lambda inv: inv.amount_cents > 5
    )
    try:
        invoice = _invoice(db_session)
        settlement.fire_invoice_paid(db_session, settlement.InvoicePaid(invoice=invoice))
        small = _invoice(db_session, number=5002, amount=5)
        settlement.fire_invoice_paid(db_session, settlement.InvoicePaid(invoice=small))
    finally:
        settlement.unsubscribe_invoice_paid("p1s_probe")
    assert seen == [invoice.id]


def test_catalogue_publisher_builds_a_valid_payload(db_session, monkeypatch, test_admin_user):
    from app.plugin_host import events as plugin_events

    emitted = []
    monkeypatch.setattr(plugin_events, "emit", lambda db, name, payload, *a, **k: emitted.append((name, payload)))
    orphan = _invoice(db_session, number=5009, billing_account_id=None)
    settlement.fire(db_session, "invoice.voided", settlement.InvoiceVoided(invoice=orphan, reason="duplicate"))
    assert emitted == []  # no billing account: billing_account_id is required, so nothing is sent
    account = BillingAccount(account_type="client", user_id=test_admin_user.id, status="active", currency="USD")
    db_session.add(account)
    db_session.flush()
    invoice = _invoice(db_session, billing_account_id=account.id)
    settlement.fire(db_session, "invoice.voided", settlement.InvoiceVoided(invoice=invoice, reason="duplicate"))
    assert emitted == [
        (
            "invoice.voided",
            {
                "invoice_id": invoice.id,
                "billing_account_id": account.id,
                "reseller_id": None,
                "invoice_number": 5001,
                "number_display": None,
                "reason": "duplicate",
            },
        )
    ]
    spec = catalogue.EVENTS["invoice.voided"]
    assert set(spec.required) <= set(emitted[0][1])


def test_account_id_falls_back_to_the_reseller_account(db_session):
    db_session.add(BillingAccount(account_type="reseller", reseller_id=41, status="active", currency="USD"))
    db_session.flush()
    account_id = settlement_subscribers.account_id_for(db_session, billing_account_id=None, reseller_id=41)
    assert account_id is not None
    assert settlement_subscribers.account_id_for(db_session, billing_account_id=9, reseller_id=41) == 9


# ------------------------------------------------------------------ numbering


def test_format_tags_and_padding():
    series = SimpleNamespace(code="CN", format="{SERIES}/{YEAR}/{MONTH}/{NUMBER}", number_padding=5, reset_period="yearly")
    assert InvoiceNumbering.format_number(series, 42, NOW) == "CN/2027/03/00042"
    assert InvoiceNumbering.period(series, NOW) == "2027"
    series.reset_period = "never"
    assert InvoiceNumbering.period(series, NOW) == ""
    series.format = "{SERIES}-{NUMBER}" + "x" * 70
    with pytest.raises(NumberingError):
        InvoiceNumbering.format_number(series, 1, NOW)


def test_invoice_number_source_uses_the_invoice_number(file_db):
    db = file_db()
    series = _series(db, code="INV", document_type=DocumentType.INVOICE, format="{NUMBER}", number_source="invoice_number")
    assert InvoiceNumbering.allocate(db, series, at=NOW, invoice_number=10042) == ("", 10042, "10042")
    with pytest.raises(NumberingError):
        InvoiceNumbering.allocate(db, series, at=NOW)
    assert db.query(InvoiceSequence).count() == 0


def test_non_gapless_counts_on_its_own_session_and_survives_rollback(file_db):
    db = file_db()
    series = _series(db)
    db.commit()
    first = InvoiceNumbering.allocate(db, series, at=NOW, session_factory=file_db)
    db.rollback()
    second = InvoiceNumbering.allocate(db, series, at=NOW, session_factory=file_db)
    assert (first, second) == (("", 1, "CN-1"), ("", 2, "CN-2"))
    check = file_db()
    assert check.get(InvoiceSequence, series_counter_name(series.id, "")).next_value == 3


def test_gapless_counts_in_the_callers_transaction(file_db):
    db = file_db()
    series = _series(db, code="DN", document_type=DocumentType.DEBIT_NOTE, gapless=True, reset_period="yearly")
    db.commit()
    assert InvoiceNumbering.allocate(db, series, at=NOW) == ("2027", 1, "DN-1")
    db.rollback()
    assert InvoiceNumbering.allocate(db, series, at=NOW) == ("2027", 1, "DN-1")
    db.commit()
    assert InvoiceNumbering.allocate(db, series, at=NOW + timedelta(days=365)) == ("2028", 1, "DN-1")


def test_default_series(file_db):
    db = file_db()
    with pytest.raises(NumberingError):
        InvoiceNumbering.default_series(db, DocumentType.CREDIT_NOTE)
    series = _series(db, default_for="credit_note")
    assert InvoiceNumbering.default_series(db, DocumentType.CREDIT_NOTE).id == series.id


# ------------------------------------------------------------------ balance


def _payment(db, invoice, amount, status, refunded=0):
    db.add(
        Payment(
            invoice_id=invoice.id, gateway="stripe", status=status, amount_cents=amount,
            refunded_cents=refunded, currency="USD", payment_metadata={},
        )
    )
    db.flush()


def test_balance_equals_legacy_allocation_on_legacy_shaped_data(db_session):
    invoice = _invoice(db_session, amount=1000)
    _payment(db_session, invoice, 600, PaymentStatus.SUCCEEDED)
    _payment(db_session, invoice, 300, PaymentStatus.FAILED)
    _payment(db_session, invoice, 400, PaymentStatus.REFUNDED, refunded=400)
    _payment(db_session, invoice, 50, PaymentStatus.PENDING)
    balance = InvoiceService.balance(db_session, invoice)
    assert balance.paid_net_cents == InvoiceService.allocated_cents(db_session, invoice.id) == 600
    assert balance.remaining_cents == 400
    assert balance.pending_cents == 50
    assert (balance.paid_gross_cents, balance.refunded_cents) == (1000, 400)


def test_balance_counts_refunded_payments_without_refunded_cents_in_full(db_session):
    invoice = _invoice(db_session, amount=1000)
    _payment(db_session, invoice, 1000, PaymentStatus.SUCCEEDED)
    # Today's refund paths set the status only, leaving refunded_cents at 0.
    _payment(db_session, invoice, 300, PaymentStatus.REFUNDED, refunded=0)
    balance = InvoiceService.balance(db_session, invoice)
    assert (balance.paid_gross_cents, balance.refunded_cents, balance.paid_net_cents) == (1300, 300, 1000)
    assert balance.paid_net_cents == InvoiceService.allocated_cents(db_session, invoice.id)
    assert balance.remaining_cents == 0


def test_balance_counts_notes_and_partial_refunds(db_session):
    invoice = _invoice(db_session, amount=1000)
    _payment(db_session, invoice, 1000, PaymentStatus.PARTIALLY_REFUNDED, refunded=250)
    for kind, total, outcome, number in (
        (AdjustmentKind.CREDIT, 200, AdjustmentOutcome.REFUNDED, 1),
        (AdjustmentKind.DEBIT, 80, AdjustmentOutcome.INCREASED_BALANCE, 2),
    ):
        db_session.add(
            InvoiceAdjustment(
                kind=kind, invoice_id=invoice.id, billing_account_id=1, series_id=1, number_value=number,
                number_display=f"N-{number}", currency="USD", subtotal_cents=total, tax_cents=0,
                total_cents=total, reason_code=AdjustmentReason.CORRECTION, outcome=outcome,
                issued_at=NOW, idempotency_key=f"note-{number}", adjustment_metadata={},
            )
        )
    db_session.flush()
    balance = InvoiceService.balance(db_session, invoice)
    assert balance.worth_cents == 1000 + 80 - 200
    assert balance.paid_net_cents == 750
    assert balance.due_cents == 130
    assert balance.remaining_cents == 130


# ------------------------------------------------------------------ shadow


def test_record_inserts_then_increments_and_reopens(file_db):
    BillingShadowService.record("credit", "c1_cache_pair", "reseller", 7, 100, 90, "topup", session_factory=file_db, now=NOW)
    db = file_db()
    row = db.query(BillingShadowMismatch).one()
    row.resolved_at = NOW
    db.commit()
    BillingShadowService.record("credit", "c1_cache_pair", "reseller", 7, 100, 80, session_factory=file_db, now=NOW + timedelta(hours=1))
    db = file_db()
    row = db.query(BillingShadowMismatch).one()
    assert (row.occurrences, row.v2_value, row.resolved_at) == (2, 80, None)
    assert row.last_seen_at.replace(tzinfo=timezone.utc) == NOW + timedelta(hours=1)


def test_record_never_raises(caplog):
    def broken():
        raise RuntimeError("database down")

    with caplog.at_level(logging.WARNING):
        BillingShadowService.record("invoice", "i1_paid_net", "invoice", 1, 1, 2, session_factory=broken)
    assert "billing shadow mismatch" in caplog.text


def test_store_scan_result_keeps_both_engines(db_session):
    BillingShadowService.store_scan_result(db_session, "credit", started_at=NOW, finished_at=NOW, subjects_checked=5, mismatches_found=0)
    value = BillingShadowService.store_scan_result(
        db_session, "invoice", started_at=NOW, finished_at=NOW, subjects_checked=9, mismatches_found=1
    )
    assert set(value["engine"]) == {"credit", "invoice"}
    assert BillingShadowService.last_scan(db_session, "invoice")["subjects_checked"] == 9


def test_engine_mode_defaults_to_shadow_and_v2_needs_the_switch_record(db_session, caplog):
    assert billing_engine_mode(db_session, "invoice") == "shadow"
    assert billing_engine_mode(db_session, "credit") == "shadow"
    set_setting(db_session, "billing_credit_engine", "v2")
    with caplog.at_level(logging.ERROR):
        assert billing_engine_mode(db_session, "credit") == "shadow"
    assert "without a matching switch record" in caplog.text
    db_session.add(SystemSetting(key="billing_engine_switch:credit", value={"mode": "v2"}))
    db_session.flush()
    assert billing_engine_mode(db_session, "credit") == "v2"


def test_appendix_a_defaults(db_session):
    expected = {
        "billing_invoice_engine": "shadow",
        "billing_credit_engine": "shadow",
        "billing_invoice_guard_mode": "log",
        "billing_shadow_min_days": 14,
        "billing_shadow_scan_hour_utc": 3,
        "billing_refund_reconcile_minutes": 5,
        "billing_proforma_enabled": False,
        "billing_credit_enabled_retail": True,
        "billing_credit_topup_min_cents": 500,
        "billing_credit_topup_max_cents": 100_000,
        "billing_credit_max_balance_cents": 0,
        "billing_credit_require_approved_order": True,
        "billing_credit_auto_apply_retail": False,
        "billing_dispute_policy_reseller": "debit_balance",
        "billing_dispute_policy_retail": "reopen_invoice",
        "billing_long_cycles_enabled": False,
        "billing_shadow_scan_enabled": False,
        "billing_refund_reconciler_enabled": False,
    }
    assert {key: get_setting(db_session, key) for key in expected} == expected


# ------------------------------------------------------------------ periodic jobs


def _job(calls, *, due=True, result=None, error=None):
    def run(db, now, dry_run):
        calls.append(dry_run)
        db.add(SystemSetting(key=f"job-wrote-{len(calls)}", value=1))
        db.flush()
        if error:
            raise error
        return result or {"ok": True}

    return periodic.PeriodicJob(
        name="p1s_test_job",
        enabled_setting="billing_refund_reconciler_enabled",
        dry_run_setting="billing_refund_reconciler_dry_run",
        interval_seconds=lambda _db: 300,
        is_due=lambda _db, _now: due,
        run=run,
    )


def _set(db, key, value):
    set_setting(db, key, value)
    db.commit()


def test_jobs_are_off_by_default_and_touch_nothing(file_db):
    calls = []
    results = periodic.run_periodic_jobs(owner="w1", now=NOW, session_factory=file_db, jobs=(_job(calls),))
    assert results == {"p1s_test_job": None} and calls == []
    assert periodic.run_periodic_jobs(owner="w1", now=NOW, session_factory=file_db) == {
        "billing_shadow_scan": None,
        "billing_refund_reconciler": None,
    }


def test_dry_run_rolls_back_and_lease_spaces_runs(file_db):
    db = file_db()
    _set(db, "billing_refund_reconciler_enabled", True)
    calls = []
    job = _job(calls)
    assert periodic.run_job(db, job, owner="w1", now=NOW) == {"ok": True}
    assert calls == [True]  # the reconciler's dry run is on by default
    assert db.get(SystemSetting, "job-wrote-1") is None
    assert periodic.run_job(db, job, owner="w2", now=NOW + timedelta(seconds=60)) is None
    _set(db, "billing_refund_reconciler_dry_run", False)
    assert periodic.run_job(db, job, owner="w2", now=NOW + timedelta(seconds=301)) == {"ok": True}
    assert file_db().get(SystemSetting, "job-wrote-2") is not None


def test_a_failing_job_is_logged_and_does_not_stop_the_others(file_db, caplog):
    db = file_db()
    _set(db, "billing_refund_reconciler_enabled", True)
    calls = []
    failing = _job(calls, error=RuntimeError("gateway down"))
    second = periodic.PeriodicJob(**{**failing.__dict__, "name": "p1s_second", "run": lambda db, now, dry: {"ran": True}})
    with caplog.at_level(logging.ERROR):
        results = periodic.run_periodic_jobs(owner="w1", now=NOW, session_factory=file_db, jobs=(failing, second))
    assert results == {"p1s_test_job": None, "p1s_second": {"ran": True}}
    assert "Periodic billing job p1s_test_job failed" in caplog.text


def test_shipped_job_bodies_are_stubs(file_db):
    db = file_db()
    with pytest.raises(NotImplementedError):
        periodic._run_refund_reconciler(db, NOW, True)
    assert periodic._run_shadow_scan(db, NOW, True)["dry_run"] is True


# ------------------------------------------------------------------ cycles


def _plan(model, cycles=(), setup=0):
    return SimpleNamespace(pricing_model=model, setup_cents=setup, cycles=list(cycles))


def _cycle(interval, price, setup=None, enabled=True, unit=None, count=None):
    from app.models.storefront import PricePlanInterval

    return SimpleNamespace(
        interval=PricePlanInterval(interval), price_cents=price, setup_cents=setup, enabled=enabled,
        interval_unit=unit, interval_count=count,
    )


def test_resolve_cycle_matches_todays_checkout_rules():
    from app.models.storefront import PricePlanPricingModel as M

    assert resolve_cycle(_plan(M.FREE, setup=500), "monthly").recurring_cents == 0
    one_time = resolve_cycle(_plan(M.ONE_TIME, [_cycle("monthly", 900)], setup=100), None)
    assert (one_time.setup_cents, one_time.recurring_cents) == (100, 900)
    plan = _plan(M.RECURRING, [_cycle("monthly", 1000), _cycle("annually", 10000, setup=0)], setup=250)
    monthly = resolve_cycle(plan, "monthly")
    assert (monthly.label, monthly.interval_unit, monthly.interval_count, monthly.months) == ("monthly", "month", 1, 1)
    assert (monthly.setup_cents, monthly.recurring_cents) == (250, 1000)
    annual = resolve_cycle(plan, "annually")
    assert (annual.interval_unit, annual.interval_count, annual.months, annual.setup_cents) == ("year", 1, 12, 0)
    for label, message in ((None, "required"), ("weekly", "Invalid"), ("biennially", "Invalid"), ("quarterly", "unavailable")):
        with pytest.raises(CycleError, match=message):
            resolve_cycle(plan, label)
    with pytest.raises(NotImplementedError):
        resolve_cycle(plan, interval_unit="year", interval_count=2)


# ------------------------------------------------------------------ locks


def test_billing_locks_new_helpers_follow_lock_order(db_session, monkeypatch):
    from app.services import billing_locks
    from app.services.billing_locks import BillingLocks

    taken = []
    original = billing_locks._lock

    def spy(db, kind, row_id, statement, **kwargs):
        taken.append(kind)
        return original(db, kind, row_id, statement, **kwargs)

    monkeypatch.setattr(billing_locks, "_lock", spy)
    account = BillingAccount(account_type="reseller", reseller_id=77, status="active", currency="USD")
    db_session.add(account)
    db_session.flush()
    invoice = _invoice(db_session, billing_account_id=account.id)
    _payment(db_session, invoice, 1000, PaymentStatus.SUCCEEDED)
    payment = db_session.query(Payment).one()
    assert BillingLocks.for_payment(db_session, payment.id, billing_account=True).id == payment.id
    assert taken == ["billing_account", "invoice", "payment"]
    order = list(billing_locks.LOCK_ORDER)
    assert [order.index(kind) for kind in taken] == sorted(order.index(kind) for kind in taken)
