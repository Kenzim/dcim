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
    ) -> TaxContext:
        raise NotImplementedError("TaxContextBuilder.for_checkout is built by the tax engine worker")

    @staticmethod
    def for_invoice(db: Session, invoice: Any, lines: Sequence[Any], now: datetime) -> TaxContext:
        raise NotImplementedError("TaxContextBuilder.for_invoice is built by the tax engine worker")

    @staticmethod
    def for_adjustment(db: Session, invoice: Any, adjustment_lines: Sequence[Any]) -> TaxContext:
        raise NotImplementedError("TaxContextBuilder.for_adjustment is built by the tax engine worker")
