"""Resolve a price plan's billing cycle: label, length, recurring price and setup fee."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.models.storefront import PricePlan, PricePlanInterval, PricePlanPricingModel
from app.services.billing.intervals import INTERVAL_MONTHS


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


def _unit_and_count(cycle) -> tuple[Optional[str], Optional[int]]:
    if cycle.interval_unit is not None and cycle.interval_count is not None:
        return cycle.interval_unit, int(cycle.interval_count)
    months = INTERVAL_MONTHS.get(getattr(cycle.interval, "value", cycle.interval))
    if months is None:
        return None, None
    if months % 12 == 0:
        return "year", months // 12
    return "month", months


def _months(unit: Optional[str], count: Optional[int]) -> Optional[int]:
    if unit is None or count is None:
        return None
    return int(count) * (12 if unit == "year" else 1)


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

    Free plans cost nothing; one-time plans use their first enabled cycle;
    recurring plans need an enabled cycle with the given label. Labels the
    renewal engine cannot yet bill (``intervals.INTERVAL_MONTHS``) are refused.
    Choosing by ``interval_unit``/``interval_count`` is added by P1-D.
    """
    if interval_unit is not None or interval_count is not None:
        raise NotImplementedError("resolve_cycle by unit and count is implemented by P1-D")
    if plan.pricing_model == PricePlanPricingModel.FREE:
        return ResolvedCycle(None, None, None, None, 0, 0)
    if plan.pricing_model == PricePlanPricingModel.ONE_TIME:
        cycle = next((c for c in plan.cycles if c.enabled), None)
        return ResolvedCycle(
            None,
            None,
            None,
            None,
            int(cycle.price_cents if cycle else 0),
            _setup_cents(plan, cycle),
        )
    if not cycle_interval:
        raise CycleError("Billing cycle is required for recurring plans")
    try:
        interval = PricePlanInterval(cycle_interval)
    except ValueError as exc:
        raise CycleError("Invalid billing cycle") from exc
    if interval.value not in INTERVAL_MONTHS:
        # Long cycles are not sold until P1-D; refused exactly as unknown labels were.
        raise CycleError("Invalid billing cycle")
    cycle = next(
        (c for c in plan.cycles if c.enabled and c.interval == interval),
        None,
    )
    if cycle is None:
        raise CycleError("Selected billing cycle is unavailable")
    unit, count = _unit_and_count(cycle)
    return ResolvedCycle(
        interval.value,
        unit,
        count,
        _months(unit, count),
        int(cycle.price_cents),
        _setup_cents(plan, cycle),
    )
