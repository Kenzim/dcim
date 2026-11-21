"""Invoice engine A1 (P1-I): publish, transition tables, recompute, void, guard, proforma, shadow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.commerce_audit import UserAuditEvent as AuditLog
from app.models.billing_series import DocumentSeries, DocumentType
from app.models.commerce_account import BillingAccount, BillingProfile, SystemSetting
from app.models.commerce_invoice_ext import InvoiceLine, InvoiceLineKind
from app.models.invoice_adjustment import (
    AdjustmentKind,
    AdjustmentOutcome,
    AdjustmentReason,
    InvoiceAdjustment,
)
from app.models.reseller import (
    Invoice,
    InvoiceDocumentType,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.services import invoice_guard
from app.services.billing import settlement
from app.services.billing_shadow import BillingShadowService
from app.services.invoice_guard import FrozenInvoiceError
from app.services.invoice_service import (
    CAUSE_SETTLEMENT,
    LEGACY_EDGES,
    V2_EDGES,
    InvoiceError,
    InvoiceService,
    InvoiceTransitionError,
    NewLine,
    compute_totals,
)
from app.services.settings_store import set_setting

S = InvoiceStatus


@pytest.fixture
def shadow_records(monkeypatch):
    records = []

    def record(engine, check_key, subject_type, subject_id, legacy, v2, context=None, **_):
        records.append(
            {"engine": engine, "check": check_key, "subject": (subject_type, subject_id),
             "legacy": legacy, "v2": v2, "context": context}
        )

    monkeypatch.setattr(BillingShadowService, "record", staticmethod(record))
    return records


@pytest.fixture
def events():
    fired = []
    names = []
    for event in (
        settlement.INVOICE_CREATED,
        settlement.INVOICE_PAID,
        settlement.INVOICE_OVERDUE,
        settlement.INVOICE_VOIDED,
        settlement.INVOICE_REFUNDED,
    ):
        name = f"p1i_test_{event}"
        settlement.subscribe(event, name, lambda db, payload, e=event: fired.append((e, payload)))
        names.append((event, name))
    yield fired
    for event, name in names:
        settlement.unsubscribe(event, name)


@pytest.fixture
def account(db_session, test_user):
    row = BillingAccount(account_type="client", user_id=test_user.id, status="active", currency="USD")
    db_session.add(row)
    db_session.flush()
    return row


def _series(db, document_type, code, **values):
    series = DocumentSeries(
        code=code,
        document_type=document_type,
        name=code,
        format="{SERIES}-{NUMBER}",
        number_padding=0,
        number_source="own",
        gapless=True,
        reset_period="never",
        enabled=True,
        default_for=document_type.value,
        **values,
    )
    db.add(series)
    db.flush()
    return series


def _v2(db):
    set_setting(db, "billing_invoice_engine", "v2")
    db.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    db.flush()
    _series(db, DocumentType.INVOICE, "INV")


def _pay(db, invoice, amount, status=PaymentStatus.SUCCEEDED, refunded=0):
    payment = Payment(
        invoice_id=invoice.id, gateway="manual", status=status, amount_cents=amount,
        refunded_cents=refunded, currency="USD", payment_metadata={},
    )
    db.add(payment)
    db.flush()
    return payment


def _note(db, invoice, kind, total, outcome, reason=AdjustmentReason.CANCEL):
    count = db.query(InvoiceAdjustment).count()
    note = InvoiceAdjustment(
        kind=kind, invoice_id=invoice.id, billing_account_id=invoice.billing_account_id, series_id=1,
        number_value=count + 1, number_display=f"N-{count + 1}", currency="USD", subtotal_cents=total,
        tax_cents=0, total_cents=total, reason_code=reason, outcome=outcome,
        issued_at=datetime.now(timezone.utc), idempotency_key=f"note-{count + 1}", adjustment_metadata={},
    )
    db.add(note)
    db.flush()
    return note


def _create(db, account, amount=1000, **kwargs):
    return InvoiceService.create(
        db,
        billing_account_id=account.id,
        purpose=InvoicePurpose.UPGRADE,
        amount_cents=amount,
        description="Upgrade",
        **kwargs,
    )


def _status_audits(db, invoice):
    return [
        (row.event_metadata or {}).get("to")
        for row in db.execute(
            select(AuditLog)
            .where(AuditLog.action == "invoice.status", AuditLog.resource_id == str(invoice.id))
            .order_by(AuditLog.id)
        ).scalars()
    ]


# ------------------------------------------------------------------ legacy and shadow


def test_shadow_create_publishes_as_today_with_snapshots_and_one_created_event(
    db_session, account, events, shadow_records
):
    db_session.add(BillingProfile(billing_account_id=account.id, legal_name="Acme Ltd", country="GB"))
    db_session.flush()
    invoice = _create(db_session, account)
    assert invoice.status == S.OPEN
    assert invoice.model_version is None
    assert (invoice.subtotal_cents, invoice.tax_cents) == (None, None)
    assert invoice.amount_cents == 1000
    assert invoice.issued_at is not None and invoice.status_changed_at is not None
    assert invoice.seller_snapshot["company_name"]
    assert invoice.buyer_snapshot["legal_name"] == "Acme Ltd"
    assert invoice.buyer_snapshot["billing_account_id"] == account.id
    assert invoice.document_type == InvoiceDocumentType.INVOICE
    lines = db_session.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).scalars().all()
    assert [(line.total_cents, line.kind) for line in lines] == [(1000, InvoiceLineKind.SETUP)]
    assert [name for name, _ in events] == [settlement.INVOICE_CREATED]
    assert _status_audits(db_session, invoice) == ["open"]
    assert shadow_records == []


def test_shadow_records_totals_difference_at_publish(db_session, account, shadow_records):
    InvoiceService.create(
        db_session,
        billing_account_id=account.id,
        purpose=InvoicePurpose.UPGRADE,
        amount_cents=1000,
        shadow_lines=[NewLine("x", 900, kind=InvoiceLineKind.PRODUCT)],
    )
    assert [r["check"] for r in shadow_records] == ["i3_totals"]
    assert shadow_records[0]["legacy"] == {"amount_cents": 1000}
    assert shadow_records[0]["v2"]["amount_cents"] == 900


def test_legacy_settlement_moves_through_transition_and_fires_paid_once(db_session, account, events):
    invoice = _create(db_session, account)
    _pay(db_session, invoice, 1000)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert invoice.status == S.PAID and invoice.paid_at is not None
    assert [name for name, _ in events].count(settlement.INVOICE_PAID) == 1
    assert _status_audits(db_session, invoice) == ["open", "paid"]


def test_shadow_records_decision_difference(db_session, account, shadow_records):
    invoice = _create(db_session, account)
    # Legacy counts only succeeded payments; v2 counts the partially refunded one net.
    _pay(db_session, invoice, 1000, PaymentStatus.PARTIALLY_REFUNDED, refunded=200)
    InvoiceService.recompute(db_session, invoice, cause="refund")
    assert invoice.status == S.OPEN
    decision = [r for r in shadow_records if r["check"] == "i1_decision"]
    assert decision and decision[0]["legacy"] == {"remaining_cents": 1000, "paid": False}
    assert decision[0]["v2"]["remaining_cents"] == 200


def test_paid_only_through_settlement(db_session, account):
    invoice = _create(db_session, account)
    with pytest.raises(InvoiceTransitionError):
        InvoiceService.transition(db_session, invoice, S.PAID, cause="admin")
    InvoiceService.transition(db_session, invoice, S.PAID, cause=CAUSE_SETTLEMENT)
    assert invoice.status == S.PAID


def test_legacy_table_refuses_v2_only_statuses(db_session, account):
    invoice = _create(db_session, account)
    for target in (S.PARTIALLY_PAID, S.COLLECTIONS, S.REFUNDED, S.DRAFT):
        with pytest.raises(InvoiceTransitionError):
            InvoiceService.transition(db_session, invoice, target, cause="test")
    assert S.PARTIALLY_PAID not in LEGACY_EDGES


def test_legacy_void_keeps_todays_edges_and_is_idempotent(db_session, account, events):
    invoice = _create(db_session, account)
    InvoiceService.void(db_session, invoice, reason="admin_void")
    InvoiceService.void(db_session, invoice, reason="again")
    assert invoice.status == S.VOID and invoice.void_reason == "admin_void"
    assert invoice.voided_at is not None
    assert settlement.INVOICE_VOIDED not in [name for name, _ in events]


def test_invoice_created_is_not_an_outbound_webhook_event(db_session, account, monkeypatch):
    from app.services.commerce_webhook_service import COMMERCE_WEBHOOK_EVENTS, CommerceWebhookService

    assert "invoice.created" not in COMMERCE_WEBHOOK_EVENTS
    calls = []
    monkeypatch.setattr(CommerceWebhookService, "enqueue", staticmethod(lambda *a, **k: calls.append(a)),
                        raising=False)
    _create(db_session, account)
    assert calls == []


def test_change_due_date_is_audited_and_refused_when_paid(db_session, account):
    invoice = _create(db_session, account)
    due = datetime(2030, 1, 2, tzinfo=timezone.utc)
    InvoiceService.change_due_date(db_session, invoice, due, actor_user_id=None)
    assert invoice.due_at.replace(tzinfo=timezone.utc) == due
    assert db_session.execute(
        select(AuditLog).where(AuditLog.action == "invoice.due_date")
    ).scalar_one() is not None
    _pay(db_session, invoice, 1000)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    with pytest.raises(InvoiceError):
        InvoiceService.change_due_date(db_session, invoice, due + timedelta(days=1))


def test_serialiser_keeps_old_fields_and_adds_balance(db_session, account):
    invoice = _create(db_session, account)
    _pay(db_session, invoice, 1000, PaymentStatus.PARTIALLY_REFUNDED, refunded=200)
    payload = InvoiceService.serialize(db_session, invoice, payments=True)
    assert (payload["allocated_cents"], payload["remaining_cents"]) == (0, 1000)
    assert payload["balance"]["remaining_cents"] == 200
    assert payload["display_number"] == str(invoice.invoice_number)
    assert payload["document_type"] == "invoice"
    assert payload["adjustments"] == [] and payload["refunds"] == []
    assert payload["payments"][0]["refunded_cents"] == 200


# ------------------------------------------------------------------ guard


def test_guard_refuses_frozen_writes_on_published_invoices(db_session, account):
    invoice = _create(db_session, account)
    invoice.amount_cents = 5
    with pytest.raises(FrozenInvoiceError):
        db_session.flush()
    db_session.rollback()


def test_guard_refuses_line_insert_and_delete(db_session, account):
    invoice = _create(db_session, account)
    db_session.add(InvoiceLine(invoice_id=invoice.id, description="late", quantity=1, unit_cents=1,
                               total_cents=1, tax_cents=0, sort_order=9))
    with pytest.raises(FrozenInvoiceError):
        db_session.flush()
    db_session.rollback()


def test_guard_refuses_invoice_delete(db_session, account):
    invoice = _create(db_session, account)
    db_session.delete(invoice)
    with pytest.raises(FrozenInvoiceError):
        db_session.flush()
    db_session.rollback()


def test_guard_allows_drafts_once_set_columns_and_free_columns(db_session, account):
    draft = _create(db_session, account, publish=False)
    draft.amount_cents = 1200
    draft.description = "changed"
    db_session.flush()
    InvoiceService.delete_draft(db_session, draft)
    invoice = _create(db_session, account)
    invoice.service_id = None
    invoice.admin_notes = "note"
    invoice.due_at = datetime(2031, 1, 1, tzinfo=timezone.utc)
    db_session.flush()
    invoice.number_display = "X-1"
    db_session.flush()
    invoice.number_display = "X-2"
    with pytest.raises(FrozenInvoiceError):
        db_session.flush()
    db_session.rollback()


def test_guard_lets_service_id_be_set_once_and_never_changed_or_cleared(db_session, account):
    from app.models.service import Service

    invoice = _create(db_session, account)
    services = []
    for name in ("a", "b"):
        service = Service(name=f"p1i-guard-{name}")
        db_session.add(service)
        services.append(service)
    db_session.flush()
    invoice.service_id = services[0].id
    db_session.commit()
    invoice.service_id = services[1].id
    with pytest.raises(FrozenInvoiceError):
        db_session.flush()
    db_session.rollback()
    invoice = db_session.get(Invoice, invoice.id)
    assert invoice.service_id == services[0].id
    invoice.service_id = None
    with pytest.raises(FrozenInvoiceError):
        db_session.flush()
    db_session.rollback()


def test_guard_log_mode_lets_the_write_through_and_records_it(db_session, account, shadow_records):
    invoice = _create(db_session, account)
    invoice_guard.set_mode_override("log")
    try:
        invoice.description = "edited"
        db_session.flush()
        db_session.commit()
    finally:
        invoice_guard.set_mode_override("enforce")
    assert [r["check"] for r in shadow_records] == ["i4_frozen_write"]
    assert shadow_records[0]["legacy"]["column"] == "description"


def test_guard_mode_follows_setting_and_is_enforce_in_v2(db_session):
    previous = invoice_guard.set_mode_override(None)
    try:
        assert invoice_guard.mode_for(db_session) == "log"
        set_setting(db_session, "billing_invoice_guard_mode", "enforce")
        assert invoice_guard.mode_for(db_session) == "enforce"
        set_setting(db_session, "billing_invoice_guard_mode", "log")
        _v2(db_session)
        assert invoice_guard.mode_for(db_session) == "enforce"
    finally:
        invoice_guard.set_mode_override(previous)


def test_guard_enforces_when_its_mode_cannot_be_read(db_session, account, monkeypatch):
    invoice = _create(db_session, account)
    previous = invoice_guard.set_mode_override(None)
    monkeypatch.setattr(invoice_guard, "_setting", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    try:
        invoice.amount_cents = 5
        with pytest.raises(FrozenInvoiceError):
            db_session.flush()
    finally:
        invoice_guard.set_mode_override(previous)
    db_session.rollback()


def test_allowance_is_only_the_proforma_conversion(db_session, account):
    with pytest.raises(ValueError):
        with invoice_guard.allow("anything", {"amount_cents"}, 1):
            pass
    invoice = _create(db_session, account)
    with invoice_guard.allow(invoice_guard.PROFORMA_CONVERSION, {"document_type"}, invoice.id):
        invoice.document_type = InvoiceDocumentType.PROFORMA
        with pytest.raises(FrozenInvoiceError):
            db_session.flush()
    db_session.rollback()


def test_allowance_covers_only_the_invoice_it_names(db_session, account):
    converted = _create(db_session, account)
    other = _create(db_session, account)
    db_session.commit()
    with invoice_guard.allow(invoice_guard.PROFORMA_CONVERSION, {"number_display"}, converted.id):
        converted.number_display = "A-1"
        db_session.flush()
        converted.number_display = "A-2"
        db_session.flush()
        other.number_display = "B-1"
        db_session.flush()
        other.number_display = "B-2"
        with pytest.raises(FrozenInvoiceError):
            db_session.flush()
    db_session.rollback()


def test_guard_refuses_moving_a_line_into_a_published_invoice(db_session, account):
    draft = _create(db_session, account, publish=False)
    published = _create(db_session, account)
    line = db_session.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == draft.id)).scalar_one()
    line.invoice_id = published.id
    with pytest.raises(FrozenInvoiceError):
        db_session.flush()
    db_session.rollback()


@pytest.fixture
def log_mode():
    previous = invoice_guard.set_mode_override("log")
    yield
    invoice_guard.set_mode_override(previous)


def test_log_mode_redacts_personal_data_and_cuts_long_values(db_session, account, shadow_records, log_mode,
                                                           caplog):
    invoice = _create(db_session, account)
    db_session.commit()
    invoice.buyer_snapshot = {"legal_name": "Secret Person", "address_line1": "1 Hidden Road"}
    invoice.currency = "EUR"
    db_session.commit()
    by_column = {r["legacy"]["column"]: r for r in shadow_records}
    assert by_column["buyer_snapshot"]["legacy"]["old"].startswith("<redacted, ")
    assert by_column["buyer_snapshot"]["v2"]["new"].startswith("<redacted, ")
    assert by_column["currency"]["v2"]["new"] == "EUR"
    assert "Secret Person" not in caplog.text and "Hidden Road" not in caplog.text
    assert invoice_guard._safe("admin_notes", "x" * 500).endswith("...")


def test_log_mode_records_nothing_for_a_rolled_back_write(db_session, account, shadow_records, log_mode):
    invoice = _create(db_session, account)
    db_session.commit()
    invoice.amount_cents = 5
    db_session.flush()
    db_session.rollback()
    db_session.commit()
    assert shadow_records == []


def test_log_mode_drops_a_rolled_back_savepoint_and_keeps_a_committed_one(
    db_session, account, shadow_records, log_mode
):
    invoice = _create(db_session, account)
    db_session.commit()
    savepoint = db_session.begin_nested()
    invoice.amount_cents = 5
    db_session.flush()
    savepoint.rollback()
    savepoint = db_session.begin_nested()
    invoice.currency = "EUR"
    db_session.flush()
    savepoint.commit()
    assert shadow_records == []  # nothing before the outer commit
    db_session.commit()
    assert [r["legacy"]["column"] for r in shadow_records] == ["currency"]


def test_zero_amount_support_does_not_cache_a_failed_read(db_session, monkeypatch):
    from app.services import invoice_service

    monkeypatch.setattr(invoice_service, "_zero_amount_support", {})
    real_execute = db_session.execute
    monkeypatch.setattr(db_session, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert invoice_service.zero_amounts_supported(db_session) is False
    assert invoice_service._zero_amount_support == {}
    monkeypatch.setattr(db_session, "execute", real_execute)
    invoice_service.zero_amounts_supported(db_session)
    assert len(invoice_service._zero_amount_support) == 1


def test_no_core_updates_of_invoices_outside_the_allowed_modules():
    import pathlib
    import re

    allowed = {"app/services/invoice_service.py", "app/services/billing_backfill.py",
               "app/services/billing_locks.py", "app/services/invoice_guard.py"}
    pattern = re.compile(r"\bupdate\(\s*(Invoice|InvoiceLine)\s*\)")
    offenders = [
        str(path) for path in pathlib.Path("app").rglob("*.py")
        if str(path) not in allowed and pattern.search(path.read_text())
    ]
    assert offenders == []


# Status writers that still bypass transition(); each owner migrates its own and
# removes it here (design 3.5).
KNOWN_DIRECT_STATUS_WRITERS = {
    "app/services/payments/refunds.py",
    "app/services/payments/orchestrator.py",
    "app/services/payments/webhooks.py",
    "app/services/reseller_billing_service.py",
    "app/services/billing/order_refunds.py",
    "app/services/recurring_billing_service.py",
}


def test_status_is_assigned_only_by_transition_and_known_writers():
    import pathlib
    import re

    pattern = re.compile(r"\.status\s*=\s*InvoiceStatus\.")
    writers = {
        str(path) for path in pathlib.Path("app").rglob("*.py")
        if pattern.search(path.read_text())
    }
    assert writers - {"app/services/invoice_service.py"} <= KNOWN_DIRECT_STATUS_WRITERS


# ------------------------------------------------------------------ v2


def test_v2_publish_computes_totals_numbers_and_settles_zero_total(db_session, account, events):
    _v2(db_session)
    lines = [
        NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT, tax_cents=150),
        NewLine("Setup", 500, kind=InvoiceLineKind.SETUP, tax_cents=50),
        NewLine("Coupon", -300, kind=InvoiceLineKind.DISCOUNT),
    ]
    invoice = _create(db_session, account, amount=1400, lines=lines)
    assert (invoice.subtotal_cents, invoice.discount_cents, invoice.tax_cents) == (1500, 300, 200)
    assert invoice.amount_cents == 1400 and invoice.model_version == 2
    assert invoice.number_display == "INV-1" and invoice.number_value == 1
    second = _create(db_session, account, amount=10, lines=[NewLine("x", 10, kind=InvoiceLineKind.FEE)])
    assert second.number_display == "INV-2"
    free = _create(db_session, account, amount=0, lines=[NewLine("free", 0, kind=InvoiceLineKind.PRODUCT)])
    assert free.status == S.PAID
    assert [name for name, payload in events if payload.invoice is free] == [
        settlement.INVOICE_CREATED, settlement.INVOICE_PAID
    ]


def test_v2_publish_refuses_bad_lines_and_mismatched_amounts(db_session, account):
    _v2(db_session)
    with pytest.raises(InvoiceError):
        _create(db_session, account, amount=999, lines=[NewLine("x", 1000, kind=InvoiceLineKind.PRODUCT)])
    db_session.rollback()
    _v2(db_session)
    with pytest.raises(InvoiceError):
        _create(db_session, account, amount=1000, lines=[NewLine("x", 1000)])
    totals = compute_totals([NewLine("d", 100, kind=InvoiceLineKind.DISCOUNT)], prices_include_tax=False)
    assert totals.problems


def test_v2_publish_needs_an_invoice_series(db_session, account):
    from app.services.invoice_numbering import NumberingError

    set_setting(db_session, "billing_invoice_engine", "v2")
    db_session.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    db_session.flush()
    with pytest.raises(NumberingError):
        _create(db_session, account)


def test_v2_status_follows_the_balance(db_session, account, events):
    _v2(db_session)
    invoice = _create(db_session, account)
    pending = _pay(db_session, invoice, 400, PaymentStatus.PENDING)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert invoice.status == S.PENDING_ACTION
    pending.status = PaymentStatus.SUCCEEDED
    db_session.flush()
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert invoice.status == S.PARTIALLY_PAID
    InvoiceService.transition(db_session, invoice, S.OVERDUE, cause="overdue_scan")
    assert [name for name, _ in events].count(settlement.INVOICE_OVERDUE) == 1
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert invoice.status == S.OVERDUE
    last = _pay(db_session, invoice, 600)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert invoice.status == S.PAID
    paid = [payload for name, payload in events if name == settlement.INVOICE_PAID]
    assert len(paid) == 1 and paid[0].payment is None
    # A full refund with its credit note: nothing paid net and nothing due.
    last.status = PaymentStatus.REFUNDED
    last.refunded_cents = 600
    pending.status = PaymentStatus.REFUNDED
    pending.refunded_cents = 400
    _note(db_session, invoice, AdjustmentKind.CREDIT, 1000, AdjustmentOutcome.REFUNDED)
    InvoiceService.recompute(db_session, invoice, cause="refund")
    assert invoice.status == S.REFUNDED
    assert [name for name, _ in events].count(settlement.INVOICE_REFUNDED) == 1
    assert _status_audits(db_session, invoice) == [
        "open", "pending_action", "partially_paid", "overdue", "paid", "refunded"
    ]


def test_v2_full_refund_without_a_credit_note_reopens_then_its_note_ends_refunded(db_session, account):
    _v2(db_session)
    invoice = _create(db_session, account)
    payment = _pay(db_session, invoice, 1000)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    payment.status = PaymentStatus.REFUNDED
    payment.refunded_cents = 1000
    db_session.flush()
    InvoiceService.recompute(db_session, invoice, cause="refund")
    assert invoice.status == S.OPEN and invoice.paid_at is not None
    _note(db_session, invoice, AdjustmentKind.CREDIT, 1000, AdjustmentOutcome.REFUNDED)
    InvoiceService.recompute(db_session, invoice, cause="credit_note")
    assert invoice.status == S.REFUNDED
    assert _status_audits(db_session, invoice)[-3:] == ["paid", "open", "refunded"]


def test_v2_credit_note_covering_an_unpaid_invoice_voids_it(db_session, account):
    _v2(db_session)
    invoice = _create(db_session, account)
    _note(db_session, invoice, AdjustmentKind.CREDIT, 1000, AdjustmentOutcome.REDUCED_BALANCE)
    InvoiceService.recompute(db_session, invoice, cause="credit_note")
    assert invoice.status == S.VOID and invoice.void_reason == "credited"


def test_v2_overpayment_goes_to_a_debit_note(db_session, account, monkeypatch, events):
    from app.services.invoice_adjustment_service import InvoiceAdjustmentService

    _v2(db_session)
    invoice = _create(db_session, account)
    calls = []

    def fake_overpayment(db, inv, excess_cents, payment):
        calls.append((inv.id, excess_cents, payment.id))
        note = InvoiceAdjustment(
            kind=AdjustmentKind.DEBIT, invoice_id=inv.id, billing_account_id=account.id, series_id=1,
            number_value=1, number_display="DN-1", currency="USD", subtotal_cents=excess_cents, tax_cents=0,
            total_cents=excess_cents, reason_code=AdjustmentReason.OVERPAYMENT,
            outcome=AdjustmentOutcome.CREDITED_TO_BALANCE, issued_at=datetime.now(timezone.utc),
            idempotency_key=f"overpayment:{inv.id}:{payment.id}", adjustment_metadata={},
        )
        db.add(note)
        db.flush()
        return note

    monkeypatch.setattr(InvoiceAdjustmentService, "issue_overpayment", staticmethod(fake_overpayment))
    _pay(db_session, invoice, 700)
    second = _pay(db_session, invoice, 700)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert calls == [(invoice.id, 400, second.id)]
    assert invoice.status == S.PAID
    assert InvoiceService.balance(db_session, invoice).due_cents == 0


def test_v2_overpayment_through_the_real_debit_note_service(db_session, account, monkeypatch, events):
    """P1-N's issue_overpayment for real; only the ledger credit (P1-C) is faked."""
    from types import SimpleNamespace

    from app.services.credit_ledger_service import CreditLedgerService

    _v2(db_session)
    _series(db_session, DocumentType.DEBIT_NOTE, "DN")
    credits = []

    def fake_credit(db, account_id, amount_cents, **kwargs):
        credits.append((account_id, amount_cents, kwargs["idempotency_key"]))
        return SimpleNamespace(id=None)

    monkeypatch.setattr(CreditLedgerService, "credit_account", staticmethod(fake_credit))
    invoice = _create(db_session, account)
    _pay(db_session, invoice, 600)
    second = _pay(db_session, invoice, 600)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=second)
    note = db_session.execute(
        select(InvoiceAdjustment).where(InvoiceAdjustment.invoice_id == invoice.id)
    ).scalar_one()
    assert (note.kind, note.total_cents, note.idempotency_key) == (
        AdjustmentKind.DEBIT, 200, f"overpayment:{invoice.id}:{second.id}"
    )
    assert note.number_display == "DN-1"
    assert credits == [(account.id, 200, f"overpayment-credit:{note.id}")]
    assert invoice.status == S.PAID
    balance = InvoiceService.balance(db_session, invoice)
    assert (balance.worth_cents, balance.due_cents) == (1200, 0)
    assert [name for name, _ in events].count(settlement.INVOICE_PAID) == 1
    # A second settlement finds nothing left to convert.
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=second)
    assert len(credits) == 1


def test_v2_money_on_a_terminal_invoice_goes_to_refund_review(db_session, account, monkeypatch):
    from app.services.billing import order_refunds

    _v2(db_session)
    invoice = _create(db_session, account)
    InvoiceService.void(db_session, invoice, reason="customer asked")
    reviews = []
    monkeypatch.setattr(order_refunds, "record_refund_review", lambda db, **kw: reviews.append(kw))
    payment = _pay(db_session, invoice, 1000)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=payment)
    assert invoice.status == S.VOID
    assert reviews and reviews[0]["payment"] is payment and reviews[0]["reason"] == "payment_on_void_invoice"


def test_v2_void_rules_keep_the_number(db_session, account, events):
    _v2(db_session)
    invoice = _create(db_session, account)
    number = invoice.number_display
    InvoiceService.void(db_session, invoice, reason="duplicate")
    assert invoice.status == S.VOID and invoice.number_display == number
    assert [name for name, _ in events].count(settlement.INVOICE_VOIDED) == 1
    paid_part = _create(db_session, account)
    _pay(db_session, paid_part, 100)
    InvoiceService.recompute(db_session, paid_part, cause="payment")
    with pytest.raises(InvoiceTransitionError):
        InvoiceService.void(db_session, paid_part, reason="x")
    with pytest.raises(InvoiceTransitionError):
        InvoiceService.transition(db_session, invoice, S.OPEN, cause="test")


def test_v2_edges_are_the_design_table():
    assert V2_EDGES[S.VOID] == V2_EDGES[S.REFUNDED] == V2_EDGES[S.FAILED] == frozenset()
    assert V2_EDGES[S.DRAFT] == {S.OPEN}
    assert S.PAID not in V2_EDGES[S.DRAFT]
    assert V2_EDGES[S.PAID] == {
        S.COLLECTIONS, S.PARTIALLY_PAID, S.OPEN, S.REFUNDED, S.PENDING_ACTION, S.VOID
    }
    assert S.OVERDUE not in V2_EDGES[S.PAID] and S.PAID in V2_EDGES[S.OPEN]


# ------------------------------------------------------------------ proforma


def test_proforma_gets_its_number_at_publish_and_an_invoice_number_when_paid(db_session, account, events):
    set_setting(db_session, "billing_proforma_enabled", True)
    _series(db_session, DocumentType.PROFORMA, "PF")
    _series(db_session, DocumentType.INVOICE, "INV")
    invoice = _create(db_session, account)
    assert invoice.document_type == InvoiceDocumentType.PROFORMA
    assert (invoice.proforma_number_display, invoice.number_display) == ("PF-1", None)
    _pay(db_session, invoice, 1000)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert invoice.status == S.PAID
    assert invoice.document_type == InvoiceDocumentType.INVOICE
    assert (invoice.proforma_number_display, invoice.number_display) == ("PF-1", "INV-1")
    assert InvoiceService.display_number(invoice) == "INV-1"


# ------------------------------------------------------------------ checkout lines


def test_checkout_v2_lines_split_tax_and_match_the_quote():
    from app.services.checkout_service import CheckoutService, QuoteLine, QuoteResult

    quote = QuoteResult(
        currency="USD", setup_cents=333, recurring_cents=667, option_delta_cents=-100,
        subtotal_cents=1000, discount_cents=100, tax_cents=161, total_cents=1061,
        coupon_code="X",
        lines=[
            QuoteLine("Setup", 1, 333, 333, kind="setup"),
            QuoteLine("Plan", 1, 767, 767, kind="product"),
            QuoteLine("Options", 1, -100, -100, kind="option"),
            QuoteLine("Coupon X", 1, -100, -100, kind="discount"),
            QuoteLine("Tax", 1, 161, 161, kind="tax"),
        ],
    )
    lines = CheckoutService._v2_invoice_lines(quote, order_item_id=7)
    assert [line.kind for line in lines] == [
        InvoiceLineKind.SETUP, InvoiceLineKind.PRODUCT, InvoiceLineKind.DISCOUNT, InvoiceLineKind.DISCOUNT
    ]
    assert sum(line.tax_cents for line in lines) == 161
    assert [line.tax_cents for line in lines][2:] == [0, 0]
    totals = compute_totals(lines, prices_include_tax=False)
    assert totals.problems == () and totals.amount_cents == quote.total_cents
    legacy = CheckoutService._legacy_invoice_lines(quote, order_item_id=7)
    assert sum(line.total_cents for line in legacy) == quote.total_cents
    assert [line.kind for line in legacy] == [None] * 5
