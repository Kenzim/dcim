"""Resolve a price plan's billing cycle: label, length, recurring price and setup fee."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.exc import UnmappedInstanceError

from app.models.storefront import PricePlan, PricePlanInterval, PricePlanPricingModel
from app.services.billing.intervals import (
    INTERVAL_MONTHS,
    months_for,
    unit_and_count,
)
from app.services.settings_store import get_setting


class CycleError(ValueError):
    """The requested cycle cannot be sold; the message is safe to show to a buyer."""


@dataclass(frozen=True)
class ResolvedCycle:
    label: Optional[str]
    interval_unit: Optional[str]
    interval_count: Optional[int]
    months: Optional[int]
    recurring_cents: int
    setup_cents: int


def long_cycles_enabled(db: Optional[Session]) -> bool:
    """Whether cycles longer than a year may be sold (``billing_long_cycles_enabled``)."""
    if db is None:
        return False
    return bool(get_setting(db, "billing_long_cycles_enabled"))


def _unit_and_count(cycle) -> tuple[Optional[str], Optional[int]]:
    if cycle.interval_unit is not None and cycle.interval_count is not None:
        return cycle.interval_unit, int(cycle.interval_count)
    months = INTERVAL_MONTHS.get(getattr(cycle.interval, "value", cycle.interval))
    if months is None:
        return None, None
    return unit_and_count(months)


def _months(unit: Optional[str], count: Optional[int]) -> Optional[int]:
    return months_for(unit, count)


def cycle_length_fields(cycle) -> dict:
    """``interval_unit``, ``interval_count``, ``months`` and ``setup_cents`` of a cycle row, for serialisers."""
    unit, count = _unit_and_count(cycle)
    return {
        "interval_unit": unit,
        "interval_count": count,
        "months": _months(unit, count),
        "setup_cents": cycle.setup_cents,
    }


def _ordered(plan: PricePlan) -> list:
    """Cycles in a fixed order (by id), whatever order the relationship loaded them in."""
    return sorted(plan.cycles, key=lambda c: (getattr(c, "id", None) is None, getattr(c, "id", None) or 0))


def _session_of(plan: PricePlan) -> Optional[Session]:
    try:
        return object_session(plan)
    except UnmappedInstanceError:
        return None


def sellable_cycles(plan: PricePlan) -> list:
    """Enabled cycles of ``plan`` in id order; cycles over a year only while long cycles are on."""
    allow_long = long_cycles_enabled(_session_of(plan))
    return [
        c
        for c in _ordered(plan)
        if c.enabled and (allow_long or (_months(*_unit_and_count(c)) or 0) <= 12)
    ]


def _setup_cents(plan: PricePlan, cycle) -> int:
    if cycle is not None and cycle.setup_cents is not None:
        return int(cycle.setup_cents)
    return int(plan.setup_cents or 0)


def resolve_cycle(
    plan: PricePlan,
    cycle_interval: Optional[str] = None,
    *,
    interval_unit: Optional[str] = None,
    interval_count: Optional[int] = None,
) -> ResolvedCycle:
    """The cycle of ``plan`` a buyer chose, with its prices.

    Free plans cost nothing; one-time plans use their first enabled cycle (lowest
    id); a one-time plan with no enabled cycle is still priced at 0. Recurring
    plans need an enabled cycle chosen by label, or by ``interval_unit`` and
    ``interval_count`` (equal lengths match, so 12 months is one year). Cycles
    longer than a year are refused unless ``billing_long_cycles_enabled`` is on
    (a plan detached from its session counts as off). The one-time branch
    takes the price of its first cycle whatever that cycle's length.
    """
    if (interval_unit is None) != (interval_count is None):
        raise CycleError("Billing cycle unit and count must be given together")
    if plan.pricing_model == PricePlanPricingModel.FREE:
        return ResolvedCycle(None, None, None, None, 0, 0)
    if plan.pricing_model == PricePlanPricingModel.ONE_TIME:
        cycle = next((c for c in _ordered(plan) if c.enabled), None)
        return ResolvedCycle(
            None,
            None,
            None,
            None,
            int(cycle.price_cents if cycle else 0),
            _setup_cents(plan, cycle),
        )
    if not cycle_interval and interval_unit is None:
        raise CycleError("Billing cycle is required for recurring plans")
    wanted_months: Optional[int] = None
    if interval_unit is not None:
        wanted_months = months_for(interval_unit, interval_count)
        if wanted_months is None:
            raise CycleError("Invalid billing cycle")
    interval: Optional[PricePlanInterval] = None
    if cycle_interval:
        try:
            interval = PricePlanInterval(cycle_interval)
        except ValueError as exc:
            raise CycleError("Invalid billing cycle") from exc
        if interval.value not in INTERVAL_MONTHS:
            raise CycleError("Invalid billing cycle")
        if wanted_months is not None and wanted_months != INTERVAL_MONTHS[interval.value]:
            raise CycleError("Billing cycle label does not match its unit and count")
    if wanted_months is None:
        wanted_months = INTERVAL_MONTHS[interval.value]
    if wanted_months > 12 and not long_cycles_enabled(_session_of(plan)):
        raise CycleError("Invalid billing cycle")
    cycle = next(
        (
            c
            for c in _ordered(plan)
            if c.enabled and _months(*_unit_and_count(c)) == wanted_months
            and (interval is None or c.interval == interval)
        ),
        None,
    )
    if cycle is None:
        raise CycleError("Selected billing cycle is unavailable")
    unit, count = _unit_and_count(cycle)
    return ResolvedCycle(
        cycle.interval.value,
        unit,
        count,
        _months(unit, count),
        int(cycle.price_cents),
        _setup_cents(plan, cycle),
    )
