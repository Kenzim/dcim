"""Shared set-up for the billing phase 2 tax tests (docs/billing-phase2-design.md 15.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from app.models.commerce_account import SystemSetting
from app.models.tax import SellerTaxRegistration, TaxClass, TaxRule
from app.services.settings_store import set_setting
from app.services.tax.context import BuyerTaxFacts, TaxContext, TaxLineInput

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)

CLASS_CODES = ("standard", "reduced", "zero", "exempt", "outside_scope", "digital_service")


def seed_classes(db) -> dict[str, int]:
    out = {}
    for code in CLASS_CODES:
        row = db.query(TaxClass).filter_by(code=code).one_or_none()
        if row is None:
            row = TaxClass(code=code, name=code, system=code != "digital_service")
            db.add(row)
            db.flush()
        out[code] = row.id
    return out


def add_rule(
    db,
    *,
    name: str = "VAT 20%",
    rate: str = "20",
    country: Optional[str] = "GB",
    region: Optional[str] = None,
    tax_class: str = "standard",
    jurisdiction: str = "ANY",
    source: str = "admin",
    rounding_point: str = "invoice_rate",
    rounding_rule: str = "round_half_up",
    valid_from: datetime = EPOCH,
    valid_to: Optional[datetime] = None,
    **extra,
) -> TaxRule:
    classes = seed_classes(db)
    rule = TaxRule(
        name=name,
        country=country,
        region=region,
        tax_class_id=classes[tax_class],
        customer_type=extra.pop("customer_type", "any"),
        rate_percent=Decimal(rate),
        priority=extra.pop("priority", 0),
        compound=extra.pop("compound", False),
        compound_order=extra.pop("compound_order", 0),
        jurisdiction=jurisdiction,
        reverse_charge=extra.pop("reverse_charge", False),
        rounding_point=rounding_point,
        rounding_rule=rounding_rule,
        valid_from=valid_from,
        valid_to=valid_to,
        enabled=extra.pop("enabled", True),
        source=source,
        **extra,
    )
    db.add(rule)
    db.flush()
    return rule


def add_registration(db, jurisdiction: str, *, country: Optional[str] = None, scheme: str = "domestic", number: str = "") -> SellerTaxRegistration:
    row = SellerTaxRegistration(
        jurisdiction=jurisdiction,
        scheme=scheme,
        tax_number=number or f"{jurisdiction}123456789",
        country=country or (jurisdiction if len(jurisdiction) == 2 else "IE"),
        valid_from=EPOCH,
        enabled=True,
    )
    db.add(row)
    db.flush()
    return row


def buyer(
    country: Optional[str] = "GB",
    *,
    business: bool = False,
    tax_id: Optional[str] = None,
    status: str = "unchecked",
    checked_at: Optional[datetime] = None,
    region: Optional[str] = None,
    postcode: Optional[str] = None,
    exempt_all: bool = False,
    exemption_ids: tuple[int, ...] = (),
    exempt_reason: Optional[str] = None,
) -> BuyerTaxFacts:
    return BuyerTaxFacts(
        country=country,
        region=region,
        postcode=postcode,
        is_business=business,
        tax_id=tax_id,
        tax_id_type="eu_vat" if tax_id else None,
        tax_id_status=status,
        tax_id_check_id=None,
        exempt_all=exempt_all,
        exemption_ids=exemption_ids,
        tax_id_checked_at=checked_at,
        exempt_reason=exempt_reason,
    )


def valid_business(country: str, *, days_old: int = 1) -> BuyerTaxFacts:
    return buyer(country, business=True, tax_id="123456789", status="valid", checked_at=NOW - timedelta(days=days_old))


def line(key: str, net: int, *, tax_class: str = "standard", kind: str = "product") -> TaxLineInput:
    return TaxLineInput(key=key, net_cents=net, tax_class_code=tax_class, kind=kind)


def context(
    lines,
    *,
    facts: Optional[BuyerTaxFacts] = None,
    purpose: str = "order_charge",
    owner: str = "retail",
    inclusive: bool = False,
    at: datetime = NOW,
    evidence: Optional[dict] = None,
) -> TaxContext:
    return TaxContext(
        tax_point_at=at,
        currency="GBP",
        prices_include_tax=inclusive,
        buyer=facts or buyer(),
        lines=tuple(lines),
        purpose=purpose,
        owner=owner,
        evidence=evidence or {},
    )


def enable_tax(db, seller_country: str = "GB") -> None:
    set_setting(db, "billing_tax_enabled", True)
    set_setting(db, "billing_tax_seller_country", seller_country)
    db.flush()


def set_engine(db, engine: str, mode: str) -> None:
    """Put ``engine`` (invoice, credit or tax) into ``mode`` with the switch record ``v2`` needs."""
    from app.services.billing_shadow import ENGINE_SETTING

    set_setting(db, ENGINE_SETTING[engine], mode)
    key = f"billing_engine_switch:{engine}"
    record = db.get(SystemSetting, key)
    if mode == "v2":
        if record is None:
            db.add(SystemSetting(key=key, value={"mode": "v2"}))
        else:
            record.value = {"mode": "v2"}
    elif record is not None:
        record.value = {"mode": mode}
    db.flush()


def invoice_v2(db) -> None:
    """Invoice engine ``v2`` with a default invoice series."""
    from app.models.billing_series import DocumentSeries, DocumentType

    set_engine(db, "invoice", "v2")
    if db.query(DocumentSeries).filter_by(code="INV").one_or_none() is None:
        db.add(
            DocumentSeries(
                code="INV", document_type=DocumentType.INVOICE, name="INV", format="{SERIES}-{NUMBER}",
                number_padding=0, number_source="own", gapless=True, reset_period="never", enabled=True,
                default_for=DocumentType.INVOICE.value,
            )
        )
    db.flush()


def capture_shadow(monkeypatch) -> list[dict]:
    """Every ``BillingShadowService.record`` call, instead of writing it."""
    from app.services.billing_shadow import BillingShadowService

    records: list[dict] = []

    def record(engine, check_key, subject_type, subject_id, legacy, v2, context=None, **_):
        records.append(
            {"engine": engine, "check": check_key, "subject": (subject_type, subject_id),
             "legacy": legacy, "v2": v2, "context": context}
        )

    monkeypatch.setattr(BillingShadowService, "record", staticmethod(record))
    return records


def retail_account(db, suffix: str = "", **profile):
    """A client billing account with a billing profile of ``profile`` fields."""
    import uuid

    from app.dao.commerce_dao import BillingAccountDAO, BillingProfileDAO
    from app.models.user import User

    tag = suffix or uuid.uuid4().hex[:8]
    user = User(username=f"p2t-{tag}", email=f"p2t-{tag}@example.test")
    db.add(user)
    db.flush()
    account = BillingAccountDAO.ensure_client_account(db, user.id)
    if profile:
        BillingProfileDAO.upsert(db, account.id, **profile)
    db.flush()
    return user, account


def stub_legal_notes(monkeypatch) -> None:
    """``render_legal_note`` is P2-P's; tests render ``<key>`` with its values."""
    import app.services.tax.legal_notes as legal_notes

    def render(_db, key, locale, **values):
        suffix = ",".join(f"{k}={v}" for k, v in sorted(values.items()))
        return f"<{key}{':' + suffix if suffix else ''}>"

    monkeypatch.setattr(legal_notes, "render_legal_note", render)
