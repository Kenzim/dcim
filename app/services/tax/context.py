"""The input of the tax engine (docs/billing-phase2-design.md 4.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class BuyerTaxFacts:
    country: Optional[str]  # billing profile country, upper case
    region: Optional[str]
    postcode: Optional[str]
    is_business: bool  # billing_profiles.is_business
    tax_id: Optional[str]
    tax_id_type: Optional[str]
    tax_id_status: str  # as stored; "valid" counts only if checked within billing_tax_id_max_age_days
    tax_id_check_id: Optional[int]
    exempt_all: bool  # billing_accounts.tax_exempt
    exemption_ids: tuple[int, ...]  # active tax_exemptions at the tax point
    tax_id_checked_at: Optional[datetime] = None  # billing_profiles.tax_id_checked_at
    exempt_reason: Optional[str] = None  # billing_accounts.tax_exempt_reason


@dataclass(frozen=True)
class TaxLineInput:
    key: str  # caller's stable id for the line ("line:0", "item:42")
    net_cents: int  # quantity * unit, after line discounts; gross when prices_include_tax
    tax_class_code: str  # "standard" when the product has none
    kind: str  # InvoiceLineKind value


@dataclass(frozen=True)
class TaxContext:
    tax_point_at: datetime
    currency: str
    prices_include_tax: bool
    buyer: BuyerTaxFacts
    lines: tuple[TaxLineInput, ...]
    purpose: str  # InvoicePurpose value or "quote"
    owner: str  # "retail" or "reseller"
    evidence: Mapping[str, Any]  # ip_country and other location evidence (7.6)


def _kind_value(kind: Any) -> str:
    value = getattr(kind, "value", kind)
    return str(value) if value else "other"


def _class_codes(db: Session) -> dict[int, str]:
    from sqlalchemy import select

    from app.models.tax import TaxClass

    return {int(i): code for i, code in db.execute(select(TaxClass.id, TaxClass.code)).all()}


def _product_class_id(db: Session, line: Any) -> Optional[int]:
    """The tax class of the product a line sells, when the line itself has none."""
    from app.models.commerce_order import OrderItem
    from app.models.product_catalog import Product
    from app.models.reseller import ServiceBilling
    from app.models.storefront import FrontendProduct

    product_id = None
    order_item_id = getattr(line, "order_item_id", None)
    if order_item_id is not None:
        item = db.get(OrderItem, int(order_item_id))
        frontend = db.get(FrontendProduct, int(item.frontend_product_id)) if item is not None else None
        product_id = frontend.product_id if frontend is not None else None
    billing_id = getattr(line, "service_billing_id", None)
    if product_id is None and billing_id is not None:
        billing = db.get(ServiceBilling, int(billing_id))
        product_id = getattr(billing, "product_id", None) if billing is not None else None
    if product_id is None:
        return None
    product = db.get(Product, int(product_id))
    return int(product.tax_class_id) if product is not None and product.tax_class_id is not None else None


def class_code_of(db: Session, tax_class_id: Optional[int]) -> str:
    """The code of a tax class id; ``standard`` for None or an unknown id."""
    if tax_class_id is None:
        return "standard"
    return _class_codes(db).get(int(tax_class_id), "standard")


def line_class_code(db: Session, line: Any, codes: Mapping[int, str]) -> str:
    """``tax_class_code`` of a line: its own class, else its product's, else ``standard``."""
    code = getattr(line, "tax_class_code", None)
    if code:
        return str(code)
    class_id = getattr(line, "tax_class_id", None)
    if class_id is None:
        class_id = _product_class_id(db, line)
    if class_id is not None and int(class_id) in codes:
        return codes[int(class_id)]
    return "standard"


def line_key(index: int, line: Any) -> str:
    sort_order = getattr(line, "sort_order", None)
    return f"line:{int(sort_order) if sort_order is not None else index}"


def line_inputs(db: Session, lines: Sequence[Any], *, purpose: Optional[str] = None) -> tuple[TaxLineInput, ...]:
    """``TaxLineInput`` for ``NewLine``, ``InvoiceLine`` or adjustment lines, keyed ``line:<sort>``.

    A ``NewLine`` has no sort order; its position is the sort order ``create``
    gives it, so a context built from the new lines and one built from the
    stored lines are equal.
    """
    codes = _class_codes(db)
    out = []
    for index, line in enumerate(lines):
        if isinstance(line, TaxLineInput):
            out.append(line)
            continue
        code = "outside_scope" if purpose == "credit_topup" else line_class_code(db, line, codes)
        out.append(
            TaxLineInput(
                key=line_key(index, line),
                net_cents=int(line.total_cents),
                tax_class_code=code,
                kind=_kind_value(getattr(line, "kind", None)),
            )
        )
    return tuple(out)


def buyer_facts(db: Session, account: Any, profile: Any, at: datetime) -> BuyerTaxFacts:
    from app.services.tax.rules_repo import active_exemption_ids

    country = (getattr(profile, "country", None) or "").strip().upper() or None
    return BuyerTaxFacts(
        country=country,
        region=(getattr(profile, "region", None) or None),
        postcode=(getattr(profile, "postal_code", None) or None),
        is_business=bool(getattr(profile, "is_business", False)),
        tax_id=(getattr(profile, "tax_id", None) or None),
        tax_id_type=getattr(profile, "tax_id_type", None),
        tax_id_status=str(getattr(profile, "tax_id_status", None) or "unchecked"),
        tax_id_check_id=getattr(profile, "tax_id_check_id", None),
        exempt_all=bool(getattr(account, "tax_exempt", False)),
        exemption_ids=active_exemption_ids(db, getattr(account, "id", None), at),
        tax_id_checked_at=getattr(profile, "tax_id_checked_at", None),
        exempt_reason=getattr(account, "tax_exempt_reason", None),
    )


def _owner_of_account(account: Any) -> str:
    kind = getattr(getattr(account, "account_type", None), "value", getattr(account, "account_type", None))
    return "reseller" if kind == "reseller" and getattr(account, "reseller_id", None) is not None else "retail"


def invoice_account(db: Session, invoice: Any) -> tuple[Any, Any]:
    """The invoice's billing account (its own, else its reseller's) and that account's profile."""
    from sqlalchemy import select

    from app.models.commerce_account import BillingAccount, BillingProfile

    account = None
    if getattr(invoice, "billing_account_id", None) is not None:
        account = db.get(BillingAccount, int(invoice.billing_account_id))
    elif getattr(invoice, "reseller_id", None) is not None:
        account = db.execute(
            select(BillingAccount).where(BillingAccount.reseller_id == invoice.reseller_id)
        ).scalar_one_or_none()
    profile = None
    if account is not None:
        profile = db.execute(
            select(BillingProfile).where(BillingProfile.billing_account_id == account.id)
        ).scalar_one_or_none()
    return account, profile


def _currency(value: Any) -> str:
    return str(value or "USD").strip().upper()


class TaxContextBuilder:
    """Builds a ``TaxContext``; reads the profile and account without locks."""

    @staticmethod
    def for_checkout(
        db: Session,
        account: Any,
        profile: Any,
        lines: Sequence[Any],
        now: datetime,
        evidence: Mapping[str, Any],
        *,
        prices_include_tax: bool = False,
    ) -> TaxContext:
        return TaxContext(
            tax_point_at=now,
            currency=_currency(getattr(account, "currency", None)),
            prices_include_tax=bool(prices_include_tax),
            buyer=buyer_facts(db, account, profile, now),
            lines=line_inputs(db, lines, purpose="order_charge"),
            purpose="order_charge",
            owner=_owner_of_account(account),
            evidence=dict(evidence or {}),
        )

    @staticmethod
    def for_invoice(db: Session, invoice: Any, lines: Sequence[Any], now: datetime) -> TaxContext:
        from app.models.commerce_order import Order

        account, profile = invoice_account(db, invoice)
        purpose = _kind_value(invoice.purpose)
        evidence: dict[str, Any] = {}
        if getattr(invoice, "order_id", None) is not None:
            order = db.get(Order, int(invoice.order_id))
            if order is not None and isinstance(order.tax_evidence, dict):
                evidence = dict(order.tax_evidence)
        return TaxContext(
            tax_point_at=now,
            currency=_currency(invoice.currency),
            prices_include_tax=bool(invoice.prices_include_tax),
            buyer=buyer_facts(db, account, profile, now),
            lines=line_inputs(db, lines, purpose=purpose),
            purpose=purpose,
            owner="reseller" if getattr(invoice, "reseller_id", None) is not None else "retail",
            evidence=evidence,
        )

    @staticmethod
    def for_adjustment(
        db: Session, invoice: Any, adjustment_lines: Sequence[Any], *, now: Optional[datetime] = None
    ) -> TaxContext:
        """A debit note taxed as a new supply at its own tax point (design 4.8), purpose ``adjustment``.

        Note line amounts are always net of tax, so the context is tax exclusive.
        """
        from datetime import timezone

        at = now or datetime.now(timezone.utc)
        account, profile = invoice_account(db, invoice)
        return TaxContext(
            tax_point_at=at,
            currency=_currency(invoice.currency),
            prices_include_tax=False,
            buyer=buyer_facts(db, account, profile, at),
            lines=line_inputs(db, adjustment_lines, purpose="adjustment"),
            purpose="adjustment",
            owner="reseller" if getattr(invoice, "reseller_id", None) is not None else "retail",
            evidence=dict(invoice.tax_evidence or {}) if isinstance(getattr(invoice, "tax_evidence", None), dict) else {},
        )
