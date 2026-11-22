"""The ``tax_rates`` read-through (docs/billing-phase2-design.md section 3).

Each ``tax_rates`` row has one current ``tax_rules`` row mirroring it
(``legacy_tax_rate_id``). The ``/tax-rates`` admin routes write ``tax_rates``
and call ``TaxRuleMirror.sync`` in the same transaction. A mirrored rule whose
``valid_from`` has passed may have been used by a publish, so a change
supersedes it from now (design 1.2): the old row is closed with
``valid_to = now`` and a new row starts at ``now``. A new ``tax_rates`` row is
mirrored from now, so no tax point before it sees the new rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.commerce_coupon_tax import TaxRate
from app.models.tax import JURISDICTION_ANY, TaxClass, TaxClassCode, TaxRule, TaxRuleSource

MIRRORED_FIELDS = ("name", "country", "region", "rate_percent", "priority", "enabled")


def rate_percent_of(rate_bps: int) -> Decimal:
    """``rate_bps / 100`` as an exact four-decimal percentage (2000 is ``20.0000``)."""
    return (Decimal(int(rate_bps)) / Decimal(100)).quantize(Decimal("0.0001"))


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _standard_class_id(db: Session) -> int:
    """The ``standard`` class (seeded by ``billing_p2_tax_seed``); created as the seed would if missing."""
    class_id = db.scalar(select(TaxClass.id).where(TaxClass.code == TaxClassCode.STANDARD.value))
    if class_id is None:
        row = TaxClass(code=TaxClassCode.STANDARD.value, name="Standard rate", system=True)
        db.add(row)
        db.flush()
        class_id = row.id
    return int(class_id)


def mirrored_values(tax_rate: TaxRate) -> dict[str, Any]:
    """The mirrored rule's fields; a region of ``''`` stays a literal region, as in ``tax_rates``."""
    return {
        "name": tax_rate.name,
        "country": (tax_rate.country or "").upper() or None,
        "region": tax_rate.region,
        "rate_percent": rate_percent_of(tax_rate.rate_bps),
        "priority": 10 if tax_rate.region else 0,
        "enabled": bool(tax_rate.enabled),
    }


def _same(rule: TaxRule, values: dict[str, Any]) -> bool:
    """Whether the rule already mirrors ``values``; a region of NULL and one of ``''`` differ (``''`` is literal)."""
    for name in MIRRORED_FIELDS:
        current = getattr(rule, name)
        wanted = values[name]
        if name == "rate_percent":
            if Decimal(str(current)) != wanted:
                return False
        elif name == "region":
            if current != wanted:
                return False
        elif isinstance(wanted, str) or wanted is None:
            if (current or None) != (wanted or None):
                return False
        elif current != wanted:
            return False
    return True


class TaxRuleMirror:
    @staticmethod
    def current(db: Session, tax_rate_id: int) -> Optional[TaxRule]:
        return db.execute(
            select(TaxRule).where(TaxRule.legacy_tax_rate_id == int(tax_rate_id))
        ).scalar_one_or_none()

    @staticmethod
    def sync(db: Session, tax_rate: TaxRate, *, now: Optional[datetime] = None) -> TaxRule:
        """Make the mirrored rule of ``tax_rate`` match it; returns the current rule row.

        Flushes; the caller commits with its ``tax_rates`` write.
        """
        at = _aware(now) or datetime.now(timezone.utc)
        db.flush()
        values = mirrored_values(tax_rate)
        rule = TaxRuleMirror.current(db, tax_rate.id)
        if rule is not None and _same(rule, values):
            return rule
        if rule is not None and _aware(rule.valid_from) > at:
            for name, value in values.items():
                setattr(rule, name, value)
            db.flush()
            return rule
        superseded_id = None
        if rule is not None:
            rule.valid_to = at
            rule.legacy_tax_rate_id = None
            superseded_id = rule.id
            db.flush()
        new = TaxRule(
            **values,
            tax_class_id=rule.tax_class_id if rule is not None else _standard_class_id(db),
            customer_type="any",
            compound=False,
            compound_order=0,
            jurisdiction=JURISDICTION_ANY,
            reverse_charge=False,
            rounding_point="invoice_rate",
            rounding_rule="tax_floor",
            valid_from=at,
            source=TaxRuleSource.TAX_RATES.value,
            legacy_tax_rate_id=tax_rate.id,
            supersedes_id=superseded_id,
        )
        db.add(new)
        db.flush()
        return new
