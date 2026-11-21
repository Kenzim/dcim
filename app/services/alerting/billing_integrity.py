"""Billing integrity detectors (spec E2): money records that disagree with each other.

Every detector only reads recorded state. They aggregate in SQL, return at most
``max_rows`` findings, and add one summary finding (resource id ``overflow``)
carrying the number of offenders that were not listed, so a large drift never
turns into thousands of alerts or a slow cycle. Money is integer minor units.

Coverage notes:

* Retail ``billing_accounts.credit_balance_cents`` has no ledger to compare with
  (``credit_ledger_entries`` is keyed by reseller only), so the credit check
  covers resellers. A reseller's billing account mirror is not compared.
* There is no refund table yet. ``billing_refund_stuck`` reads a table named
  ``refunds`` with ``id``, ``status`` and ``created_at`` columns when it exists
  and reports nothing otherwise, so it is correct before and after refunds are
  recorded in their own table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import MetaData, Table, and_, case, func, inspect, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.commerce_invoice_ext import InvoiceLine
from app.models.invoice_adjustment import AdjustmentKind, InvoiceAdjustment, InvoiceAdjustmentLine
from app.models.reseller import (
    CreditLedgerEntry,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
)
from app.services.alerting.catalogue import Finding

logger = logging.getLogger(__name__)

OVERFLOW_ID = "overflow"
_REFUNDS_TABLE = "refunds"
_PENDING = "pending"


def _limited(db: Session, stmt: Select, max_rows: int) -> tuple[list[Any], int]:
    """Rows of an ordered statement up to ``max_rows``, and how many more there are."""
    rows = list(db.execute(stmt.limit(max_rows)).all())
    if len(rows) < max_rows:
        return rows, 0
    total = db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar() or 0
    return rows, max(0, int(total) - len(rows))


def _overflow(alert_type: str, resource_type: str, noun: str, remaining: int, shown: int) -> Finding:
    return Finding(
        alert_type,
        resource_type,
        OVERFLOW_ID,
        "more",
        f"{remaining} more {noun} not listed",
        f"Only the first {shown} are listed; fix those and the rest will follow.",
        details={"remaining": remaining, "shown": shown},
    )


def _cutoff(now: datetime, params: dict, key: str, unit: str) -> datetime:
    return now - timedelta(**{unit: params[key]})


def _money(value: Any) -> int:
    return int(value or 0)


def _checked_invoices() -> list[InvoiceStatus]:
    """Statuses whose totals are expected to be settled and consistent."""
    return [InvoiceStatus.OPEN, InvoiceStatus.PAID, InvoiceStatus.OVERDUE, InvoiceStatus.PENDING_ACTION, InvoiceStatus.FAILED]


def _invoice_filters(params: dict, now: datetime, statuses: list[InvoiceStatus]) -> list:
    return [
        Invoice.status.in_(statuses),
        Invoice.created_at >= _cutoff(now, params, "lookback_days", "days"),
        Invoice.created_at <= _cutoff(now, params, "min_age_minutes", "minutes"),
    ]


def invoice_lines_mismatch(db: Session, params: dict, now: datetime) -> list[Finding]:
    """An invoice that has lines whose totals do not add up to the invoice total.

    Invoices without lines are skipped (reseller invoices often have none), as are
    draft and void invoices.
    """
    line_sum = func.sum(InvoiceLine.total_cents)
    stmt = (
        select(Invoice.id, Invoice.invoice_number, Invoice.amount_cents, Invoice.currency, line_sum.label("lines"),
               func.count(InvoiceLine.id).label("line_count"))
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(*_invoice_filters(params, now, _checked_invoices()))
        .group_by(Invoice.id, Invoice.invoice_number, Invoice.amount_cents, Invoice.currency)
        .having(line_sum != Invoice.amount_cents)
        .order_by(Invoice.id)
    )
    rows, remaining = _limited(db, stmt, params["max_rows"])
    out = [
        Finding(
            "billing_invoice_lines_mismatch",
            "invoice",
            str(row.id),
            f"Invoice {row.invoice_number}",
            f"Invoice {row.invoice_number} lines do not add up to its total",
            f"The {row.line_count} lines add up to {_money(row.lines)} but the invoice total is "
            f"{_money(row.amount_cents)} ({row.currency}, minor units).",
            details={
                "invoice_id": row.id,
                "invoice_number": int(row.invoice_number),
                "amount_cents": _money(row.amount_cents),
                "lines_total_cents": _money(row.lines),
                "difference_cents": _money(row.lines) - _money(row.amount_cents),
                "currency": row.currency,
            },
        )
        for row in rows
    ]
    if remaining:
        out.append(_overflow("billing_invoice_lines_mismatch", "invoice", "invoices with mismatched lines",
                             remaining, len(rows)))
    return out


def _succeeded_sum_stmt(params: dict, now: datetime, statuses: list[InvoiceStatus]):
    paid = func.coalesce(func.sum(Payment.amount_cents), 0)
    base = (
        select(Invoice.id, Invoice.invoice_number, Invoice.amount_cents, Invoice.currency, paid.label("paid"))
        .select_from(Invoice)
        .outerjoin(Payment, and_(Payment.invoice_id == Invoice.id, Payment.status == PaymentStatus.SUCCEEDED))
        .where(*_invoice_filters(params, now, statuses))
        .group_by(Invoice.id, Invoice.invoice_number, Invoice.amount_cents, Invoice.currency)
        .order_by(Invoice.id)
    )
    return base, paid


def paid_invoice_without_payment(db: Session, params: dict, now: datetime) -> list[Finding]:
    """A paid invoice whose succeeded payments do not cover its total.

    Refunded payments have the refunded status and so no longer count.
    """
    base, paid = _succeeded_sum_stmt(params, now, [InvoiceStatus.PAID])
    rows, remaining = _limited(db, base.having(paid < Invoice.amount_cents), params["max_rows"])
    out = [
        Finding(
            "billing_paid_without_payment",
            "invoice",
            str(row.id),
            f"Invoice {row.invoice_number}",
            f"Paid invoice {row.invoice_number} is not covered by payments",
            f"Succeeded payments total {_money(row.paid)} against an invoice total of {_money(row.amount_cents)} "
            f"({row.currency}, minor units).",
            details={
                "invoice_id": row.id,
                "invoice_number": int(row.invoice_number),
                "amount_cents": _money(row.amount_cents),
                "succeeded_payments_cents": _money(row.paid),
                "shortfall_cents": _money(row.amount_cents) - _money(row.paid),
                "currency": row.currency,
            },
        )
        for row in rows
    ]
    if remaining:
        out.append(_overflow("billing_paid_without_payment", "invoice", "paid invoices without enough payments",
                             remaining, len(rows)))
    return out


def invoice_overallocated(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Succeeded payments that add up to more than the invoice total."""
    base, paid = _succeeded_sum_stmt(params, now, [s for s in InvoiceStatus if s != InvoiceStatus.DRAFT])
    rows, remaining = _limited(db, base.having(paid > Invoice.amount_cents), params["max_rows"])
    out = [
        Finding(
            "billing_overallocated",
            "invoice",
            str(row.id),
            f"Invoice {row.invoice_number}",
            f"Invoice {row.invoice_number} has more payments than its total",
            f"Succeeded payments total {_money(row.paid)} against an invoice total of {_money(row.amount_cents)} "
            f"({row.currency}, minor units).",
            details={
                "invoice_id": row.id,
                "invoice_number": int(row.invoice_number),
                "amount_cents": _money(row.amount_cents),
                "succeeded_payments_cents": _money(row.paid),
                "excess_cents": _money(row.paid) - _money(row.amount_cents),
                "currency": row.currency,
            },
        )
        for row in rows
    ]
    if remaining:
        out.append(_overflow("billing_overallocated", "invoice", "overpaid invoices", remaining, len(rows)))
    return out


def credit_ledger_drift(db: Session, params: dict, now: datetime) -> list[Finding]:
    """A reseller whose cached balance disagrees with its ledger.

    The ledger amounts are signed, so they must add up to the cached balance, and
    the newest entry's ``balance_after_cents`` must equal it too. Resellers with no
    ledger entries are not checked.
    """
    del now
    sums = (
        select(CreditLedgerEntry.reseller_id.label("reseller_id"), func.sum(CreditLedgerEntry.amount_cents).label("total"),
               func.max(CreditLedgerEntry.id).label("latest_id"), func.count(CreditLedgerEntry.id).label("entries"))
        .group_by(CreditLedgerEntry.reseller_id)
        .subquery()
    )
    stmt = (
        select(Reseller.id, Reseller.user_id, Reseller.cached_balance_cents, sums.c.total, sums.c.entries,
               CreditLedgerEntry.balance_after_cents)
        .select_from(Reseller)
        .join(sums, sums.c.reseller_id == Reseller.id)
        .join(CreditLedgerEntry, CreditLedgerEntry.id == sums.c.latest_id)
        .where(or_(sums.c.total != Reseller.cached_balance_cents,
                   CreditLedgerEntry.balance_after_cents != Reseller.cached_balance_cents))
        .order_by(Reseller.id)
    )
    rows, remaining = _limited(db, stmt, params["max_rows"])
    out = []
    for row in rows:
        cached, total, latest = _money(row.cached_balance_cents), _money(row.total), _money(row.balance_after_cents)
        out.append(
            Finding(
                "billing_credit_drift",
                "reseller",
                str(row.id),
                f"Reseller {row.id}",
                f"Credit balance of reseller {row.id} disagrees with its ledger",
                f"Cached balance {cached}, ledger entries add up to {total}, newest entry says {latest} (minor units).",
                details={
                    "reseller_id": row.id,
                    "cached_balance_cents": cached,
                    "ledger_sum_cents": total,
                    "latest_balance_after_cents": latest,
                    "ledger_entries": int(row.entries),
                },
            )
        )
    if remaining:
        out.append(_overflow("billing_credit_drift", "reseller", "resellers with credit drift", remaining, len(rows)))
    return out


def orphaned_payment(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Pending payments nobody is going to finish.

    Two cases: a pending payment with a gateway reference that has stayed pending for
    ``pending_after_minutes`` (the gateway never confirmed it), and a pending payment
    whose invoice is already paid or void for ``settled_grace_minutes``.
    """
    old = Payment.created_at <= _cutoff(now, params, "pending_after_minutes", "minutes")
    settled = and_(
        Invoice.status.in_([InvoiceStatus.PAID, InvoiceStatus.VOID]),
        Payment.created_at <= _cutoff(now, params, "settled_grace_minutes", "minutes"),
    )
    stmt = (
        select(Payment.id, Payment.invoice_id, Payment.gateway, Payment.amount_cents, Payment.currency,
               Payment.external_ref, Payment.created_at, Invoice.invoice_number, Invoice.status.label("invoice_status"))
        .join(Invoice, Invoice.id == Payment.invoice_id)
        .where(Payment.status == PaymentStatus.PENDING,
               or_(and_(Payment.external_ref.isnot(None), Payment.external_ref != "", old), settled))
        .order_by(Payment.id)
    )
    rows, remaining = _limited(db, stmt, params["max_rows"])
    out = []
    for row in rows:
        reasons = []
        if row.external_ref and row.created_at is not None and _naive(row.created_at) <= _cutoff(
            now, params, "pending_after_minutes", "minutes"
        ):
            reasons.append("pending_with_gateway_reference")
        status = getattr(row.invoice_status, "value", row.invoice_status)
        if status in (InvoiceStatus.PAID.value, InvoiceStatus.VOID.value):
            reasons.append(f"invoice_{status}")
        out.append(
            Finding(
                "billing_orphaned_payment",
                "payment",
                str(row.id),
                f"Payment {row.id} on invoice {row.invoice_number}",
                f"Payment {row.id} has been pending too long",
                f"{row.gateway} payment of {_money(row.amount_cents)} ({row.currency}, minor units) is still pending: "
                + ", ".join(reasons).replace("_", " ") + ".",
                details={
                    "payment_id": row.id,
                    "invoice_id": row.invoice_id,
                    "invoice_number": int(row.invoice_number),
                    "invoice_status": status,
                    "gateway": row.gateway,
                    "amount_cents": _money(row.amount_cents),
                    "currency": row.currency,
                    "external_ref": row.external_ref,
                    "reasons": reasons,
                },
            )
        )
    if remaining:
        out.append(_overflow("billing_orphaned_payment", "payment", "orphaned payments", remaining, len(rows)))
    return out


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _refunds_table(db: Session) -> Table | None:
    bind = db.get_bind()
    if not inspect(bind).has_table(_REFUNDS_TABLE):
        return None
    table = Table(_REFUNDS_TABLE, MetaData(), autoload_with=bind)
    if not {"id", "status", "created_at"} <= set(table.c.keys()):
        logger.warning("Table %s lacks id, status or created_at; stuck refunds are not checked", _REFUNDS_TABLE)
        return None
    return table


def refund_stuck(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Refunds that have been pending longer than ``pending_after_minutes``."""
    table = _refunds_table(db)
    if table is None:
        return []
    stmt = (
        select(*(table.c[name] for name in ("id", "status", "created_at") + _optional(table)))
        .where(table.c.status == _PENDING, table.c.created_at <= _cutoff(now, params, "pending_after_minutes", "minutes"))
        .order_by(table.c.id)
    )
    rows, remaining = _limited(db, stmt, params["max_rows"])
    out = []
    for row in rows:
        data = row._mapping
        details = {"refund_id": int(data["id"])}
        for name in _optional(table):
            value = data[name]
            details[name] = int(value) if isinstance(value, (int,)) and not isinstance(value, bool) else (
                None if value is None else str(value)
            )
        details["created_at"] = _naive(data["created_at"]).isoformat() if data["created_at"] is not None else None
        out.append(
            Finding(
                "billing_refund_stuck",
                "refund",
                str(data["id"]),
                f"Refund {data['id']}",
                f"Refund {data['id']} is still pending",
                "A refund was started but has not been confirmed or failed; check the gateway before retrying.",
                details=details,
            )
        )
    if remaining:
        out.append(_overflow("billing_refund_stuck", "refund", "stuck refunds", remaining, len(rows)))
    return out


def _optional(table: Table) -> tuple[str, ...]:
    return tuple(name for name in ("payment_id", "invoice_id", "amount_cents", "currency") if name in table.c)


# Billing phase 1 detectors (docs/billing-phase1-design.md 10.4). Each is a
# read-only stub returning no findings until its owner fills it in, and each
# alert type ships disabled.


def shadow_mismatch(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Open rows in ``billing_shadow_mismatches`` (P1-C)."""
    return []


def shadow_scan_stale(db: Session, params: dict, now: datetime) -> list[Finding]:
    """An engine in ``shadow`` whose last daily scan is over 26 hours old (P1-C)."""
    return []


def ledger_chain_broken(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Billing accounts whose ledger chain fails property P4 (P1-C)."""
    return []


def negative_retail_balance(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Retail billing accounts with a balance below zero (P1-C)."""
    return []


_PUBLISHED = [status for status in InvoiceStatus if status != InvoiceStatus.DRAFT]


def invoice_totals(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Model v2 invoices whose totals fail property P1 (design 1.3.1).

    The stored subtotal, discount and tax must equal the sums over the lines,
    and ``amount_cents`` must equal subtotal minus discount plus tax (tax not
    added when prices include it) and never be negative.
    """
    from sqlalchemy import case

    from app.models.commerce_invoice_ext import InvoiceLineKind

    discount_kind = InvoiceLineKind.DISCOUNT
    subtotal = func.coalesce(
        func.sum(case((InvoiceLine.kind != discount_kind, InvoiceLine.total_cents), else_=0)), 0
    )
    discount = func.coalesce(
        func.sum(case((InvoiceLine.kind == discount_kind, -InvoiceLine.total_cents), else_=0)), 0
    )
    tax = func.coalesce(func.sum(InvoiceLine.tax_cents), 0)
    stmt = (
        select(
            Invoice.id,
            Invoice.invoice_number,
            Invoice.amount_cents,
            Invoice.subtotal_cents,
            Invoice.discount_cents,
            Invoice.tax_cents,
            Invoice.prices_include_tax,
            Invoice.currency,
            subtotal.label("line_subtotal"),
            discount.label("line_discount"),
            tax.label("line_tax"),
        )
        .select_from(Invoice)
        .outerjoin(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(Invoice.model_version == 2, *_invoice_filters(params, now, _PUBLISHED))
        .group_by(
            Invoice.id,
            Invoice.invoice_number,
            Invoice.amount_cents,
            Invoice.subtotal_cents,
            Invoice.discount_cents,
            Invoice.tax_cents,
            Invoice.prices_include_tax,
            Invoice.currency,
        )
        .having(
            or_(
                Invoice.subtotal_cents.is_(None),
                Invoice.discount_cents.is_(None),
                Invoice.tax_cents.is_(None),
                Invoice.amount_cents < 0,
                subtotal != Invoice.subtotal_cents,
                discount != Invoice.discount_cents,
                tax != Invoice.tax_cents,
                Invoice.amount_cents
                != Invoice.subtotal_cents
                - Invoice.discount_cents
                + case((Invoice.prices_include_tax.is_(True), 0), else_=Invoice.tax_cents),
            )
        )
        .order_by(Invoice.id)
    )
    rows, remaining = _limited(db, stmt, params["max_rows"])
    out = []
    for row in rows:
        expected = (
            _money(row.line_subtotal)
            - _money(row.line_discount)
            + (0 if row.prices_include_tax else _money(row.line_tax))
        )
        out.append(
            Finding(
                "billing_invoice_totals",
                "invoice",
                str(row.id),
                f"Invoice {row.invoice_number}",
                f"Invoice {row.invoice_number} totals do not add up",
                f"Lines give subtotal {_money(row.line_subtotal)}, discount {_money(row.line_discount)} "
                f"and tax {_money(row.line_tax)}, a total of {expected}; the invoice stores "
                f"{_money(row.amount_cents)} ({row.currency}, minor units).",
                details={
                    "invoice_id": row.id,
                    "invoice_number": int(row.invoice_number),
                    "amount_cents": _money(row.amount_cents),
                    "subtotal_cents": row.subtotal_cents,
                    "discount_cents": row.discount_cents,
                    "tax_cents": row.tax_cents,
                    "line_subtotal_cents": _money(row.line_subtotal),
                    "line_discount_cents": _money(row.line_discount),
                    "line_tax_cents": _money(row.line_tax),
                    "expected_amount_cents": expected,
                    "currency": row.currency,
                },
            )
        )
    if remaining:
        out.append(_overflow("billing_invoice_totals", "invoice", "v2 invoices whose totals do not add up",
                             remaining, len(rows)))
    return out


def invoice_status(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Model v2 invoices whose status disagrees with their balance, property P3 (design 3.2).

    Payments and notes are aggregated per invoice in SQL; the status rule is
    applied to each row. Terminal invoices (void, refunded, failed) are not
    checked, and an overpaid invoice is always reported (it must have been
    converted to account credit).
    """
    from types import SimpleNamespace

    from sqlalchemy import case

    from app.models.invoice_adjustment import AdjustmentKind, InvoiceAdjustment
    from app.services.invoice_service import TERMINAL_V2, InvoiceBalance, InvoiceService

    paid_statuses = (PaymentStatus.SUCCEEDED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED)
    refunded = case(
        (and_(Payment.status == PaymentStatus.REFUNDED, Payment.refunded_cents == 0), Payment.amount_cents),
        (Payment.refunded_cents > Payment.amount_cents, Payment.amount_cents),
        else_=Payment.refunded_cents,
    )
    payments = (
        select(
            Payment.invoice_id.label("invoice_id"),
            func.coalesce(func.sum(case((Payment.status.in_(paid_statuses), Payment.amount_cents), else_=0)), 0)
            .label("paid_gross"),
            func.coalesce(func.sum(case((Payment.status.in_(paid_statuses), refunded), else_=0)), 0)
            .label("refunded"),
            func.coalesce(
                func.sum(case((Payment.status == PaymentStatus.PENDING, Payment.amount_cents), else_=0)), 0
            ).label("pending"),
        )
        .group_by(Payment.invoice_id)
        .subquery()
    )
    notes = (
        select(
            InvoiceAdjustment.invoice_id.label("invoice_id"),
            func.coalesce(
                func.sum(case((InvoiceAdjustment.kind == AdjustmentKind.DEBIT, InvoiceAdjustment.total_cents),
                              else_=0)), 0
            ).label("debit"),
            func.coalesce(
                func.sum(case((InvoiceAdjustment.kind == AdjustmentKind.CREDIT, InvoiceAdjustment.total_cents),
                              else_=0)), 0
            ).label("credit"),
        )
        .group_by(InvoiceAdjustment.invoice_id)
        .subquery()
    )
    checked = [status for status in _PUBLISHED if status not in TERMINAL_V2]
    stmt = (
        select(
            Invoice.id,
            Invoice.invoice_number,
            Invoice.status,
            Invoice.amount_cents,
            Invoice.currency,
            func.coalesce(payments.c.paid_gross, 0).label("paid_gross"),
            func.coalesce(payments.c.refunded, 0).label("refunded"),
            func.coalesce(payments.c.pending, 0).label("pending"),
            func.coalesce(notes.c.debit, 0).label("debit"),
            func.coalesce(notes.c.credit, 0).label("credit"),
        )
        .select_from(Invoice)
        .outerjoin(payments, payments.c.invoice_id == Invoice.id)
        .outerjoin(notes, notes.c.invoice_id == Invoice.id)
        .where(Invoice.model_version == 2, *_invoice_filters(params, now, checked))
        .order_by(Invoice.id)
    )
    out: list[Finding] = []
    remaining = 0
    for row in db.execute(stmt):
        total = _money(row.amount_cents)
        worth = total + _money(row.debit) - _money(row.credit)
        paid_net = _money(row.paid_gross) - _money(row.refunded)
        due = worth - paid_net
        balance = InvoiceBalance(
            total, _money(row.debit), _money(row.credit), worth, _money(row.paid_gross),
            _money(row.refunded), paid_net, _money(row.pending), due, max(0, due),
        )
        expected = InvoiceService.target_status(
            db, SimpleNamespace(status=row.status), balance, cause="integrity_scan"
        )
        if due >= 0 and expected == row.status:
            continue
        if len(out) >= params["max_rows"]:
            remaining += 1
            continue
        expected_text = "account credit for the overpayment" if due < 0 else expected.value
        out.append(
            Finding(
                "billing_invoice_status",
                "invoice",
                str(row.id),
                f"Invoice {row.invoice_number}",
                f"Invoice {row.invoice_number} is {row.status.value}, its balance says {expected_text}",
                f"Worth {worth}, paid {paid_net} net, pending {_money(row.pending)}, due {due} "
                f"({row.currency}, minor units).",
                details={
                    "invoice_id": row.id,
                    "invoice_number": int(row.invoice_number),
                    "status": row.status.value,
                    "expected_status": None if due < 0 else expected.value,
                    "balance": balance.as_dict(),
                    "currency": row.currency,
                },
            )
        )
    if remaining:
        out.append(_overflow("billing_invoice_status", "invoice", "v2 invoices whose status disagrees",
                             remaining, len(out)))
    return out


def _issued_numbers():
    """Every legal number issued: (series_id, period, value) from invoices, proformas and notes."""
    from sqlalchemy import literal, union_all

    from app.models.invoice_adjustment import InvoiceAdjustment

    return union_all(
        select(
            Invoice.series_id.label("series_id"),
            func.coalesce(Invoice.number_period, literal("")).label("period"),
            Invoice.number_value.label("value"),
        ).where(Invoice.series_id.is_not(None), Invoice.number_value.is_not(None)),
        select(
            Invoice.proforma_series_id.label("series_id"),
            func.coalesce(Invoice.proforma_number_period, literal("")).label("period"),
            Invoice.proforma_number_value.label("value"),
        ).where(Invoice.proforma_series_id.is_not(None), Invoice.proforma_number_value.is_not(None)),
        select(
            InvoiceAdjustment.series_id.label("series_id"),
            func.coalesce(InvoiceAdjustment.number_period, literal("")).label("period"),
            InvoiceAdjustment.number_value.label("value"),
        ).where(InvoiceAdjustment.series_id.is_not(None), InvoiceAdjustment.number_value.is_not(None)),
    ).subquery()


def numbering_gaps(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Gapless series with a gap or a duplicate number, property P7 (design 5.1).

    Per (series, period) the issued values are clean when their count, their
    distinct count and their maximum are equal and the minimum is 1.
    """
    from app.models.billing_series import DocumentSeries

    issued = _issued_numbers()
    count = func.count(issued.c.value)
    distinct = func.count(func.distinct(issued.c.value))
    smallest = func.min(issued.c.value)
    largest = func.max(issued.c.value)
    stmt = (
        select(
            DocumentSeries.id,
            DocumentSeries.code,
            issued.c.period,
            count.label("issued"),
            distinct.label("distinct"),
            smallest.label("smallest"),
            largest.label("largest"),
        )
        .select_from(issued)
        .join(DocumentSeries, DocumentSeries.id == issued.c.series_id)
        .where(DocumentSeries.gapless.is_(True))
        .group_by(DocumentSeries.id, DocumentSeries.code, issued.c.period)
        .having(or_(count != distinct, distinct != largest, smallest != 1))
        .order_by(DocumentSeries.id, issued.c.period)
    )
    rows, remaining = _limited(db, stmt, params["max_rows"])
    out = []
    for row in rows:
        issued_count, distinct_count = int(row.issued), int(row.distinct)
        duplicates = issued_count - distinct_count
        missing = max(0, int(row.largest) - distinct_count)
        period = row.period or "all time"
        problems = []
        if duplicates:
            problems.append(f"{duplicates} repeated")
        if missing:
            problems.append(f"{missing} missing")
        out.append(
            Finding(
                "billing_numbering_gaps",
                "document_series",
                f"{row.id}:{row.period}",
                f"Series {row.code}",
                f"Series {row.code} ({period}) has {' and '.join(problems)} numbers",
                f"{issued_count} numbers issued, {distinct_count} distinct, from {row.smallest} "
                f"to {row.largest}; a gapless series must issue exactly 1 to its largest number once each.",
                details={
                    "series_id": row.id,
                    "code": row.code,
                    "period": row.period,
                    "issued": issued_count,
                    "distinct": distinct_count,
                    "min_value": int(row.smallest),
                    "max_value": int(row.largest),
                    "duplicates": duplicates,
                    "missing": missing,
                },
            )
        )
    if remaining:
        out.append(_overflow("billing_numbering_gaps", "document_series",
                             "series periods with gaps or repeats", remaining, len(rows)))
    return out


def refund_mismatch(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Payments whose refunded_cents disagrees with their succeeded refunds, property P5 (P1-R)."""
    return []


def adjustment_bounds(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Invoices worth less than zero, or notes whose lines do not sum to their totals (P1-N).

    One finding per invoice; a note with no lines counts as lines that sum to zero.
    """
    debits = func.coalesce(func.sum(case((InvoiceAdjustment.kind == AdjustmentKind.DEBIT, InvoiceAdjustment.total_cents), else_=0)), 0)
    credits = func.coalesce(func.sum(case((InvoiceAdjustment.kind == AdjustmentKind.CREDIT, InvoiceAdjustment.total_cents), else_=0)), 0)
    worth_stmt = (
        select(Invoice.id, Invoice.invoice_number, Invoice.amount_cents, Invoice.currency,
               debits.label("debits"), credits.label("credits"))
        .join(InvoiceAdjustment, InvoiceAdjustment.invoice_id == Invoice.id)
        .group_by(Invoice.id, Invoice.invoice_number, Invoice.amount_cents, Invoice.currency)
        .having(Invoice.amount_cents + debits - credits < 0)
        .order_by(Invoice.id)
    )
    line_totals = func.coalesce(func.sum(InvoiceAdjustmentLine.total_cents), 0)
    line_tax = func.coalesce(func.sum(InvoiceAdjustmentLine.tax_cents), 0)
    lines_stmt = (
        select(InvoiceAdjustment.id.label("note_id"), InvoiceAdjustment.invoice_id, InvoiceAdjustment.number_display,
               InvoiceAdjustment.subtotal_cents, InvoiceAdjustment.tax_cents,
               line_totals.label("line_subtotal"), line_tax.label("line_tax"))
        .outerjoin(InvoiceAdjustmentLine, InvoiceAdjustmentLine.adjustment_id == InvoiceAdjustment.id)
        .group_by(InvoiceAdjustment.id, InvoiceAdjustment.invoice_id, InvoiceAdjustment.number_display,
                  InvoiceAdjustment.subtotal_cents, InvoiceAdjustment.tax_cents)
        .having(or_(line_totals != InvoiceAdjustment.subtotal_cents, line_tax != InvoiceAdjustment.tax_cents))
        .order_by(InvoiceAdjustment.id)
    )
    max_rows = params["max_rows"]
    worth_rows, worth_remaining = _limited(db, worth_stmt, max_rows)
    line_rows, line_remaining = _limited(db, lines_stmt, max_rows)

    details: dict[int, dict] = {}
    for row in worth_rows:
        details.setdefault(row.id, {"invoice_id": row.id})["worth_cents"] = (
            _money(row.amount_cents) + _money(row.debits) - _money(row.credits)
        )
    for row in line_rows:
        entry = details.setdefault(row.invoice_id, {"invoice_id": row.invoice_id})
        entry.setdefault("notes_with_mismatched_lines", []).append(
            {
                "note_id": row.note_id,
                "number": row.number_display,
                "note_subtotal_cents": _money(row.subtotal_cents),
                "lines_subtotal_cents": _money(row.line_subtotal),
                "note_tax_cents": _money(row.tax_cents),
                "lines_tax_cents": _money(row.line_tax),
            }
        )
    out = []
    for invoice_id in sorted(details):
        entry = details[invoice_id]
        problems = []
        if "worth_cents" in entry:
            problems.append(f"it is worth {entry['worth_cents']} after its notes")
        if "notes_with_mismatched_lines" in entry:
            problems.append("a note's lines do not add up to its totals")
        out.append(
            Finding(
                "billing_adjustment_bounds",
                "invoice",
                str(invoice_id),
                f"Invoice {invoice_id}",
                f"Invoice {invoice_id} has out-of-bounds credit or debit notes",
                "; ".join(problems).capitalize() + ".",
                details=entry,
            )
        )
    remaining = worth_remaining + line_remaining
    if remaining:
        out.append(_overflow("billing_adjustment_bounds", "invoice", "invoices with out-of-bounds notes",
                             remaining, len(out)))
    return out


BILLING_DETECTORS = {
    "billing_invoice_lines_mismatch": invoice_lines_mismatch,
    "billing_paid_without_payment": paid_invoice_without_payment,
    "billing_overallocated": invoice_overallocated,
    "billing_credit_drift": credit_ledger_drift,
    "billing_orphaned_payment": orphaned_payment,
    "billing_refund_stuck": refund_stuck,
    "billing_shadow_mismatch": shadow_mismatch,
    "billing_shadow_scan_stale": shadow_scan_stale,
    "billing_ledger_chain_broken": ledger_chain_broken,
    "billing_negative_retail_balance": negative_retail_balance,
    "billing_invoice_totals": invoice_totals,
    "billing_invoice_status": invoice_status,
    "billing_numbering_gaps": numbering_gaps,
    "billing_refund_mismatch": refund_mismatch,
    "billing_adjustment_bounds": adjustment_bounds,
}
