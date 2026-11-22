"""Rule selection for the tax engine (docs/billing-phase2-design.md 4.3 step 4).

Reads only: rules, classes, registrations and exemptions, never with a lock,
so a publish never locks a rule row (design 1.2 and 15.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.tax import (
    JURISDICTION_ANY,
    SellerTaxRegistration,
    TaxClass,
    TaxExemption,
    TaxRule,
    TaxRuleSource,
)

RATE_SCALE = 10_000  # rate_ppm = rate_percent * 10_000
MAX_RATE_DECIMALS = 4

# Specificity of a rule: the most specific match wins (postcode, region, country, none).
SPEC_POSTCODE = 3
SPEC_REGION = 2
SPEC_COUNTRY = 1
SPEC_NONE = 0


class TaxRateError(ValueError):
    """A rate that is not an exact percentage with at most four decimals."""


def rate_ppm(rate_percent: object) -> int:
    """``rate_percent`` as integer parts per million of the base (20% is 200,000).

    The rate is converted once with ``Decimal``; a rate with more than four
    decimals, a negative rate or one above 100 is refused.
    """
    try:
        value = rate_percent if isinstance(rate_percent, Decimal) else Decimal(str(rate_percent))
    except (InvalidOperation, ValueError):
        raise TaxRateError(f"rate {rate_percent!r} is not a number") from None
    if not value.is_finite():
        raise TaxRateError(f"rate {rate_percent!r} is not a number")
    scaled = value * RATE_SCALE
    if scaled != scaled.to_integral_value():
        raise TaxRateError(f"rate {rate_percent!r} has more than {MAX_RATE_DECIMALS} decimals")
    ppm = int(scaled)
    if ppm < 0 or ppm > 100 * RATE_SCALE:
        raise TaxRateError(f"rate {rate_percent!r} is outside 0 to 100")
    return ppm


def normalise_rate(rate_percent: object) -> Decimal:
    """The rate as a four-decimal ``Decimal`` (``"20"`` becomes ``Decimal("20.0000")``)."""
    return (Decimal(rate_ppm(rate_percent)) / RATE_SCALE).quantize(Decimal("0.0001"))


def normalise_postcode(postcode: Optional[str]) -> Optional[str]:
    if not postcode:
        return None
    return re.sub(r"\s+", "", postcode).upper() or None


def _same_text(left: Optional[str], right: Optional[str]) -> bool:
    return (left or "").strip().casefold() == (right or "").strip().casefold()


@dataclass(frozen=True)
class RuleView:
    """The fields of one ``tax_rules`` row the engine uses, read once."""

    id: int
    name: str
    country: Optional[str]
    region: Optional[str]
    postcode_pattern: Optional[str]
    tax_class_id: int
    customer_type: str
    rate_percent: Decimal
    rate_ppm: int
    priority: int
    compound: bool
    compound_order: int
    jurisdiction: str
    reverse_charge: bool
    reverse_charge_zone: Optional[str]
    legal_note_key: Optional[str]
    rounding_point: str
    rounding_rule: str
    source: str
    version: str

    @property
    def mirrored(self) -> bool:
        return self.source == TaxRuleSource.TAX_RATES.value

    def specificity(self, buyer_region: Optional[str] = None) -> tuple[int, int]:
        """``(level, rank)``; the highest wins.

        A rule mirrored from ``tax_rates`` keeps that table's lookup: a region
        row (``''`` included, as a literal region) is regional, and a buyer
        with no region prefers the national row over any region row.
        """
        if self.postcode_pattern:
            return SPEC_POSTCODE, 0
        if self.mirrored and self.region is not None:
            return (SPEC_REGION, 0) if (buyer_region or "") else (SPEC_COUNTRY, -1)
        if self.region:
            return SPEC_REGION, 0
        if self.country:
            return SPEC_COUNTRY, 0
        return SPEC_NONE, 0

    def matches_place(self, country: Optional[str], region: Optional[str], postcode: Optional[str]) -> bool:
        if self.country and self.country.upper() != (country or "").upper():
            return False
        if self.mirrored and self.region is not None:
            # tax_rates: a buyer with no region may take a region row.
            if (region or "") and not _same_text(self.region, region):
                return False
        elif self.region and not _same_text(self.region, region):
            return False
        if self.postcode_pattern:
            normalised = normalise_postcode(postcode)
            if normalised is None:
                return False
            try:
                if re.fullmatch(self.postcode_pattern, normalised) is None:
                    return False
            except re.error:
                return False
        return True

    def matches_customer(self, is_business: bool) -> bool:
        if self.customer_type == "any":
            return True
        return self.customer_type == ("business" if is_business else "consumer")


def _value(enum_or_str: object) -> str:
    return str(getattr(enum_or_str, "value", enum_or_str))


def _version(row: object) -> str:
    stamp = getattr(row, "updated_at", None)
    return stamp.isoformat() if stamp is not None else ""


def _view(rule: TaxRule) -> RuleView:
    mirrored = _value(rule.source) == TaxRuleSource.TAX_RATES.value
    return RuleView(
        id=int(rule.id),
        name=rule.name,
        country=(rule.country or None) and rule.country.upper(),
        region=rule.region if mirrored else (rule.region or None),
        postcode_pattern=rule.postcode_pattern or None,
        tax_class_id=int(rule.tax_class_id),
        customer_type=_value(rule.customer_type),
        rate_percent=normalise_rate(rule.rate_percent),
        rate_ppm=rate_ppm(rule.rate_percent),
        priority=int(rule.priority or 0),
        compound=bool(rule.compound),
        compound_order=int(rule.compound_order or 0),
        jurisdiction=rule.jurisdiction or JURISDICTION_ANY,
        reverse_charge=bool(rule.reverse_charge),
        reverse_charge_zone=rule.reverse_charge_zone or None,
        legal_note_key=rule.legal_note_key or None,
        rounding_point=_value(rule.rounding_point),
        rounding_rule=rule.rounding_rule,
        source=_value(rule.source),
        version=_version(rule),
    )


def class_ids(db: Session) -> dict[str, int]:
    """Tax class code to id."""
    return {code: int(id_) for id_, code in db.execute(select(TaxClass.id, TaxClass.code)).all()}


def rules_valid_at(
    db: Session,
    at: datetime,
    *,
    class_ids: Iterable[int],
    jurisdictions: Optional[Sequence[str]] = None,
    mirrored_only: bool = False,
) -> list[RuleView]:
    """Enabled rules of the given classes valid at ``at`` (``valid_from <= at < valid_to``).

    ``jurisdictions`` limits the rows to those jurisdictions; ``mirrored_only``
    to the rows mirrored from ``tax_rates`` with jurisdiction ``ANY``. Ordered by
    id so the caller sees a stable list.
    """
    ids = sorted(set(int(i) for i in class_ids))
    if not ids:
        return []
    stmt = select(TaxRule).where(
        TaxRule.enabled.is_(True),
        TaxRule.tax_class_id.in_(ids),
        TaxRule.valid_from <= at,
        or_(TaxRule.valid_to.is_(None), TaxRule.valid_to > at),
    )
    if mirrored_only:
        stmt = stmt.where(
            TaxRule.source == TaxRuleSource.TAX_RATES.value,
            TaxRule.jurisdiction == JURISDICTION_ANY,
        )
    elif jurisdictions is not None:
        stmt = stmt.where(TaxRule.jurisdiction.in_(sorted(set(jurisdictions))))
    rows = db.execute(stmt.order_by(TaxRule.id)).scalars().all()
    views = []
    for row in rows:
        try:
            views.append(_view(row))
        except (TaxRateError, ValueError):
            # A rate the API would refuse (written around it) never applies.
            continue
    return views


def select_rules(
    candidates: Sequence[RuleView],
    *,
    tax_class_id: int,
    countries: Mapping[str, Optional[str]],
    region: Optional[str],
    postcode: Optional[str],
    is_business: bool,
    reverse_charge: Optional[bool] = None,
) -> list[RuleView]:
    """The rules that apply to one line, in compound order (design 4.3 step 4).

    ``countries`` maps each jurisdiction that may apply to the country its
    rules are matched against; rules of any other jurisdiction are left out.
    Most specific wins (postcode, region, country, none), then the highest
    priority, then a rule of a registration's jurisdiction before an ``ANY``
    rule, then the lowest id. Mirrored rules carry no priority of their own
    (``tax_rates`` has none), so they tie on id alone. Compound rules of that
    same specificity are all applied after it, in ``compound_order``.
    ``reverse_charge`` limits the candidates to reverse charge rules (True)
    or ordinary rules (False).
    """
    matching = [
        rule
        for rule in candidates
        if rule.tax_class_id == tax_class_id
        and rule.jurisdiction in countries
        and (reverse_charge is None or rule.reverse_charge == reverse_charge)
        and rule.matches_customer(is_business)
        and rule.matches_place(countries[rule.jurisdiction], region, postcode)
    ]
    if not matching:
        return []
    best = max(rule.specificity(region) for rule in matching)
    level = [rule for rule in matching if rule.specificity(region) == best]
    plain = sorted(
        (r for r in level if not r.compound),
        key=lambda r: (0 if r.mirrored else -r.priority, r.jurisdiction == JURISDICTION_ANY, r.id),
    )
    compound = [r for r in level if r.compound]
    chosen = plain[:1] + compound
    return sorted(chosen, key=lambda r: (r.compound, r.compound_order, r.id))


@dataclass(frozen=True)
class RegistrationView:
    id: int
    jurisdiction: str
    scheme: str
    tax_number: str
    country: str
    invoice_profile_id: Optional[int]
    invoice_series_id: Optional[int]
    version: str


def registrations_valid_at(db: Session, at: datetime) -> list[RegistrationView]:
    rows = db.execute(
        select(SellerTaxRegistration)
        .where(
            SellerTaxRegistration.enabled.is_(True),
            SellerTaxRegistration.valid_from <= at,
            or_(SellerTaxRegistration.valid_to.is_(None), SellerTaxRegistration.valid_to > at),
        )
        .order_by(SellerTaxRegistration.id)
    ).scalars().all()
    return [
        RegistrationView(
            id=int(row.id),
            jurisdiction=(row.jurisdiction or "").upper(),
            scheme=_value(row.scheme),
            tax_number=row.tax_number,
            country=(row.country or "").upper(),
            invoice_profile_id=row.invoice_profile_id,
            invoice_series_id=row.invoice_series_id,
            version=_version(row),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class ExemptionView:
    id: int
    reason: str
    tax_class_ids: tuple[int, ...]  # empty: every class
    version: str

    def covers(self, tax_class_id: Optional[int]) -> bool:
        return not self.tax_class_ids or (tax_class_id is not None and tax_class_id in self.tax_class_ids)


def exemptions_by_id(db: Session, ids: Iterable[int], at: datetime) -> list[ExemptionView]:
    """The account exemptions among ``ids`` active at ``at`` (not revoked, in their validity)."""
    wanted = sorted(set(int(i) for i in ids))
    if not wanted:
        return []
    rows = db.execute(
        select(TaxExemption)
        .where(
            TaxExemption.id.in_(wanted),
            TaxExemption.scope == "account",
            TaxExemption.revoked_at.is_(None),
            TaxExemption.valid_from <= at,
            or_(TaxExemption.valid_to.is_(None), TaxExemption.valid_to > at),
        )
        .order_by(TaxExemption.id)
    ).scalars().all()
    return [
        ExemptionView(
            id=int(row.id),
            reason=row.reason,
            tax_class_ids=tuple(int(i) for i in (row.tax_class_ids or [])),
            version=_version(row),
        )
        for row in rows
    ]


def active_exemption_ids(db: Session, billing_account_id: Optional[int], at: datetime) -> tuple[int, ...]:
    if billing_account_id is None:
        return ()
    rows = db.execute(
        select(TaxExemption.id)
        .where(
            TaxExemption.billing_account_id == int(billing_account_id),
            TaxExemption.scope == "account",
            TaxExemption.revoked_at.is_(None),
            TaxExemption.valid_from <= at,
            or_(TaxExemption.valid_to.is_(None), TaxExemption.valid_to > at),
        )
        .order_by(TaxExemption.id)
    ).scalars().all()
    return tuple(int(i) for i in rows)
