"""Consistency checks of a published invoice's frozen tax (design 5 ``t6_scan``, 15.4 detectors)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Iterator, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tax import InvoiceLineTax

logger = logging.getLogger(__name__)


def _key(name: Any, rate: Any, jurisdiction: Any, reverse_charge: Any) -> tuple:
    return (str(name), str(Decimal(str(rate)).quantize(Decimal("0.0001"))), str(jurisdiction), bool(reverse_charge))


def line_sum_differences(db: Session, invoice_id: int, lines: Sequence[Any]) -> list[tuple[int, int, int]]:
    """``(line_id, line_tax, rows_tax)`` for each line whose tax is not the sum of its rows."""
    sums: dict[int, int] = {}
    for line_id, tax in db.execute(
        select(InvoiceLineTax.invoice_line_id, InvoiceLineTax.tax_cents).where(InvoiceLineTax.invoice_id == invoice_id)
    ).all():
        sums[int(line_id)] = sums.get(int(line_id), 0) + int(tax)
    return [
        (int(line.id), int(line.tax_cents or 0), sums.get(int(line.id), 0))
        for line in lines
        if int(line.tax_cents or 0) != sums.get(int(line.id), 0)
    ]


def summary_from_rows(db: Session, invoice_id: int) -> dict[tuple, list[int]]:
    out: dict[tuple, list[int]] = {}
    for row in db.scalars(select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice_id)):
        entry = out.setdefault(_key(row.name, row.rate_percent, row.jurisdiction, row.reverse_charge), [0, 0])
        entry[0] += int(row.base_cents)
        entry[1] += int(row.tax_cents)
    return out


def summary_stored(invoice: Any) -> dict[tuple, list[int]]:
    out: dict[tuple, list[int]] = {}
    for entry in invoice.tax_summary or []:
        key = _key(entry.get("name"), entry.get("rate_percent", 0), entry.get("jurisdiction"),
                   entry.get("reverse_charge"))
        totals = out.setdefault(key, [0, 0])
        totals[0] += int(entry.get("base_cents") or 0)
        totals[1] += int(entry.get("tax_cents") or 0)
    return out


def _jsonable(summary: dict[tuple, list[int]]) -> list:
    return [[*key, base, tax] for key, (base, tax) in sorted(summary.items())]


def invoice_tax_findings(db: Session, invoice: Any, lines: Sequence[Any], *, rerun: bool) -> Iterator[tuple]:
    """``(stored, expected, context)`` for each inconsistency of an engine-taxed invoice.

    ``rerun`` also recalculates with the engine at ``tax_point_at`` on the
    stored lines; a provider's result is not re-run (its rows are compared
    with each other only).
    """
    for line_id, line_tax, rows_tax in line_sum_differences(db, invoice.id, lines):
        yield {"line_tax_cents": line_tax}, {"rows_tax_cents": rows_tax}, f"line_sum:line:{line_id}"
    rows = summary_from_rows(db, invoice.id)
    stored = summary_stored(invoice)
    if rows != stored:
        yield {"summary": _jsonable(stored)}, {"rows": _jsonable(rows)}, "summary"
    if not rerun or invoice.tax_point_at is None or str(invoice.tax_engine or "").startswith("provider:"):
        return
    from app.services.tax.context import TaxContextBuilder, line_key
    from app.services.tax.engine import TaxEngine

    try:
        result = TaxEngine.calculate(db, TaxContextBuilder.for_stored(db, invoice, lines))
    except Exception:  # noqa: BLE001 - one invoice never stops the scan
        logger.exception("tax scan: could not recalculate invoice %s", invoice.id)
        yield {"tax_cents": int(invoice.tax_cents or 0)}, {"error": "recalculation_failed"}, "rerun"
        return
    stored_lines = {line_key(index, line): int(line.tax_cents or 0) for index, line in enumerate(lines)}
    again = {key: int(value.tax_cents) for key, value in result.lines.items()}
    if any(stored_lines.get(key, 0) != again.get(key, 0) for key in set(stored_lines) | set(again)):
        yield {"lines": stored_lines}, {"lines": again}, "rerun"
