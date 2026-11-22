"""Tax on invoices at create and publish (docs/billing-phase2-design.md 4.5, 4.7, 4.9).

``InvoiceService`` calls these under its locks; everything here is the pure
path (``TaxEngine.calculate``), so no network call happens under a lock.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.tax import InvoiceLineTax, SellerTaxRegistration, TaxRule
from app.services.tax import shadow as tax_shadow
from app.services.tax.context import TaxContextBuilder, line_key
from app.services.tax.engine import LEGACY_TAXED_PURPOSES, SCOPE_OUT, TaxEngine
from app.services.tax.result import TaxResult

logger = logging.getLogger(__name__)

ENGINE_LEGACY = "legacy"

TAX_RULES_CHANGED_MESSAGE = "Tax rates changed while this invoice was being issued; please try again"
# The 409 detail of a route that turns ``TaxRuleChanged`` into a conflict.
TAX_RULES_CHANGED_DETAIL = {"code": "tax_rules_changed", "message": TAX_RULES_CHANGED_MESSAGE}


class TaxRuleChanged(RuntimeError):
    """A rule the result uses was closed or superseded by the tax point (design 4.9).

    Retryable: the caller rolls back and runs its whole transaction again,
    which reads the rules afresh. Recalculating in the same transaction would
    read the same old snapshot on MariaDB (REPEATABLE READ).
    """

    def __init__(self, rule_ids: Sequence[int]) -> None:
        self.rule_ids = tuple(rule_ids)
        super().__init__(f"tax rules {', '.join(map(str, self.rule_ids))} changed before the tax point")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _purpose(invoice: Any) -> str:
    return str(getattr(invoice.purpose, "value", invoice.purpose))


def effective_mode(db: Session, invoice: Any) -> str:
    """The tax mode for one invoice: ``v2`` needs a v2 invoice (the switch requires engine v2)."""
    from app.services.tax.mode import tax_engine_mode

    mode = tax_engine_mode(db)
    if mode == "v2" and invoice.model_version != 2:
        return "shadow"
    return mode


def calculate_verified(
    db: Session, invoice: Any, lines: Sequence[Any], now: datetime, given: Optional[TaxResult], where: str
) -> TaxResult:
    """The pure result under the lock; ``given`` is used only when its context hash still matches.

    A ``given`` result whose hash differs is discarded and ``t5_context_changed``
    recorded (design 4.7).
    """
    pure = TaxEngine.calculate(db, TaxContextBuilder.for_invoice(db, invoice, lines, now))
    if given is None or given.context_hash == pure.context_hash:
        return given or pure
    tax_shadow.record_context_changed(int(invoice.id), given, pure, where)
    return pure


def rule_lock_statement(ids: Sequence[int]):
    """``SELECT id, valid_from, valid_to ... WHERE id IN (ids) ORDER BY id LOCK IN SHARE MODE``.

    Filtered on the primary key alone and read through it on MySQL and
    MariaDB, so the rows are locked in id order whatever other indexes exist.
    """
    statement = (
        select(TaxRule.id, TaxRule.valid_from, TaxRule.valid_to)
        .where(TaxRule.id.in_(sorted(ids)))
        .order_by(TaxRule.id)
        .with_for_update(read=True)
    )
    for dialect in ("mysql", "mariadb"):
        statement = statement.with_hint(TaxRule, "FORCE INDEX (PRIMARY)", dialect_name=dialect)
    return statement


def lock_rules(db: Session, result: TaxResult, at: datetime) -> None:
    """Share-lock the rules ``result`` uses, in id order, and check them at the tax point ``at``.

    One locking read by primary key, so every publish and every import that
    locks rules in ascending id order takes them in the same order. A locking
    read returns the latest committed rows whatever the snapshot, and every
    supersede closes the old row (``valid_to``) under its own lock, so a rule
    closed after this transaction read it is seen here. A rule not in force
    at ``at``, or with an enabled successor in force by then, raises
    ``TaxRuleChanged``.
    """
    ids = sorted({int(p.rule_id) for line in result.lines.values() for p in line.parts if p.rule_id is not None})
    if not ids:
        return
    at = _aware(at)
    rows = db.execute(rule_lock_statement(ids)).all()
    changed = set(ids) - {int(row.id) for row in rows}
    changed.update(
        int(row.id)
        for row in rows
        if _aware(row.valid_from) > at or (row.valid_to is not None and _aware(row.valid_to) <= at)
    )
    # A plain read: a locking read on supersedes_id could scan and lock rows out of id order.
    changed.update(
        int(old_id)
        for old_id in db.execute(
            select(TaxRule.supersedes_id).where(
                TaxRule.supersedes_id.in_(ids), TaxRule.enabled.is_(True), TaxRule.valid_from <= at
            )
        ).scalars()
    )
    if changed:
        raise TaxRuleChanged(sorted(changed))


def write_rows(db: Session, invoice: Any, lines: Sequence[Any], result: TaxResult, at: datetime) -> None:
    """Replace the draft's ``invoice_line_taxes`` and set each line's ``tax_cents`` from ``result``.

    The rules are locked and checked at the tax point ``at`` before the first
    insert (``lock_rules``).
    """
    lock_rules(db, result, at)
    db.execute(delete(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice.id))
    for index, line in enumerate(lines):
        line_tax = result.line(line_key(index, line))
        parts = line_tax.parts if line_tax is not None else ()
        line.tax_cents = sum(int(p.tax_cents) for p in parts)
        for position, part in enumerate(parts):
            db.add(
                InvoiceLineTax(
                    invoice_id=invoice.id,
                    invoice_line_id=line.id,
                    tax_rule_id=part.rule_id,
                    position=position,
                    name=part.name[:255],
                    rate_percent=part.rate_percent,
                    jurisdiction=part.jurisdiction[:16],
                    base_cents=int(part.base_cents),
                    tax_cents=int(part.tax_cents),
                    reverse_charge=bool(part.reverse_charge),
                    legal_note_key=part.legal_note_key,
                    source=part.source[:128],
                )
            )
    db.flush()


def legal_notes_json(db: Session, result: TaxResult, locale: Optional[str]) -> list[dict]:
    from app.services.tax.legal_notes import render_legal_note

    values = result.evidence.values.get("legal_note_values") or {}
    out = []
    for key in result.legal_note_keys:
        text = render_legal_note(db, key, locale or "en", **dict(values.get(key) or {}))
        out.append({"key": key, "text": text})
    return out


def registration_of(db: Session, result: TaxResult) -> Optional[SellerTaxRegistration]:
    if result.registration_id is None:
        return None
    return db.get(SellerTaxRegistration, int(result.registration_id))


def check_currency(db: Session, invoice: Any, result: TaxResult) -> None:
    """A UK-registered taxed invoice is in GBP unless ``billing_tax_allow_foreign_currency_gb``."""
    from app.services.settings_store import get_setting

    registration = registration_of(db, result)
    if registration is None or (registration.jurisdiction or "").upper() != "GB":
        return
    if result.tax_cents <= 0 or str(invoice.currency).upper() == "GBP":
        return
    if get_setting(db, "billing_tax_allow_foreign_currency_gb"):
        return
    from app.services.invoice_service import InvoiceError

    raise InvoiceError(
        "a UK VAT invoice must be in GBP; set billing_tax_allow_foreign_currency_gb to print a GBP conversion"
    )


def stamp_published(db: Session, invoice: Any, result: TaxResult, now: datetime) -> None:
    """The frozen tax fields of a v2 publish (design 4.9); call before the snapshots are taken."""
    check_currency(db, invoice, result)
    invoice.tax_engine = result.engine
    invoice.tax_point_at = now
    invoice.tax_summary = result.summary_json()
    evidence = dict(result.evidence.values)
    evidence["scope"] = result.scope
    evidence["context_hash"] = result.context_hash
    invoice.tax_evidence = evidence
    invoice.legal_notes = legal_notes_json(db, result, invoice.locale)
    invoice.seller_registration_id = result.registration_id


def _invoice_profile(db: Session, registration: Optional[SellerTaxRegistration]):
    """The registration's invoice profile, else the enabled profile named ``default``."""
    from app.models.tax import InvoiceProfile

    if registration is not None and registration.invoice_profile_id is not None:
        row = db.get(InvoiceProfile, int(registration.invoice_profile_id))
        if row is not None and row.enabled:
            return row
    return db.execute(
        select(InvoiceProfile).where(InvoiceProfile.name == "default", InvoiceProfile.enabled.is_(True))
    ).scalar_one_or_none()


def snapshot_extras(db: Session, invoice: Any, seller: dict, buyer: dict) -> None:
    """Registration number on the seller block, tax ID facts on the buyer block (v2 only)."""
    from app.services.tax.context import invoice_account

    registration = (
        db.get(SellerTaxRegistration, int(invoice.seller_registration_id))
        if invoice.seller_registration_id is not None
        else None
    )
    seller["registration_tax_number"] = registration.tax_number if registration is not None else None
    seller["registration_jurisdiction"] = registration.jurisdiction if registration is not None else None
    profile_row = _invoice_profile(db, registration)
    seller["document_title"] = profile_row.document_title if profile_row is not None else "Invoice"
    seller["proforma_title"] = profile_row.proforma_title if profile_row is not None else None
    seller["credit_note_title"] = profile_row.credit_note_title if profile_row is not None else None
    seller["footer_text"] = profile_row.footer_text if profile_row is not None else None
    _account, profile = invoice_account(db, invoice)
    buyer["buyer_tax_id"] = getattr(profile, "tax_id", None)
    buyer["buyer_tax_id_status"] = getattr(profile, "tax_id_status", None)
    buyer["buyer_is_business"] = bool(getattr(profile, "is_business", False))


def shadow_publish(db: Session, invoice: Any, lines: Sequence[Any], now: datetime) -> None:
    """``t2_uncovered`` for a purpose legacy never taxes (design 4.7); writes nothing."""
    purpose = _purpose(invoice)
    if purpose in LEGACY_TAXED_PURPOSES:
        return
    try:
        result = TaxEngine.calculate(db, TaxContextBuilder.for_invoice(db, invoice, lines, now))
    except Exception:  # noqa: BLE001 - a shadow check never fails a publish
        logger.exception("tax shadow: could not calculate invoice %s", invoice.id)
        return
    if result.scope == SCOPE_OUT:
        return
    tax_shadow.record_uncovered(int(invoice.id), purpose, result)


def stored_summary(db: Session, invoice_id: int) -> list[tuple]:
    """``(name, rate, jurisdiction, reverse_charge, base, tax)`` summed from the stored rows."""
    totals: dict[tuple, list[int]] = {}
    rows = db.execute(
        select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice_id).order_by(InvoiceLineTax.id)
    ).scalars()
    for row in rows:
        key = (row.name, str(row.rate_percent), row.jurisdiction, bool(row.reverse_charge))
        entry = totals.setdefault(key, [0, 0])
        entry[0] += int(row.base_cents)
        entry[1] += int(row.tax_cents)
    return sorted((*key, base, tax) for key, (base, tax) in totals.items())


def proforma_rate_check(db: Session, invoice: Any, lines: Sequence[Any], at: datetime) -> None:
    """``t3_proforma_rate_changed`` when the rates at payment differ from those at publish (4.5)."""
    if invoice.tax_engine in (None, ENGINE_LEGACY):
        return
    try:
        result = TaxEngine.calculate(db, TaxContextBuilder.for_invoice(db, invoice, lines, at))
    except Exception:  # noqa: BLE001 - a shadow check never fails a conversion
        logger.exception("tax shadow: could not recalculate proforma %s", invoice.id)
        return
    published = sorted(
        (e["name"], str(e["rate_percent"]), e["jurisdiction"], bool(e["reverse_charge"]))
        for e in (invoice.tax_summary or [])
    )
    now_rates = sorted(
        (e["name"], str(e["rate_percent"]), e["jurisdiction"], bool(e["reverse_charge"]))
        for e in result.summary_json()
    )
    if published != now_rates:
        tax_shadow.record_proforma_rate_changed(
            int(invoice.id), {"summary": invoice.tax_summary or []}, {"summary": result.summary_json()}
        )
