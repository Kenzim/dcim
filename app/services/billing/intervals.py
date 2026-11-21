"""Billing cycle intervals and calendar-month advance with a stable anchor day."""

from __future__ import annotations

import calendar
from datetime import datetime
from typing import Optional

INTERVAL_MONTHS = {
    "monthly": 1,
    "quarterly": 3,
    "semiannually": 6,
    "annually": 12,
    "biennially": 24,
    "triennially": 36,
}
# Labels longer than a year are only sold while ``billing_long_cycles_enabled`` is on;
# every process still understands them so a service already on one is never misread.
LONG_INTERVALS = frozenset({"biennially", "triennially"})
DEFAULT_INTERVAL = "monthly"


def is_long_interval(label: Optional[str]) -> bool:
    return label in LONG_INTERVALS


def unit_and_count(months: int) -> tuple[str, int]:
    """The canonical (``month``|``year``, count) for a whole number of months."""
    if months % 12 == 0:
        return "year", months // 12
    return "month", months


def label_for_months(months: int) -> Optional[str]:
    for label, value in INTERVAL_MONTHS.items():
        if value == months:
            return label
    return None


def months_for(unit: Optional[str], count: Optional[int]) -> Optional[int]:
    if unit not in ("month", "year") or count is None or int(count) < 1:
        return None
    return int(count) * (12 if unit == "year" else 1)


def normalise_interval(interval: Optional[str]) -> str:
    """Lower-cased label; empty or ``None`` means monthly, an unknown label raises ``ValueError``."""
    value = (interval or "").strip().lower() or DEFAULT_INTERVAL
    if value not in INTERVAL_MONTHS:
        raise ValueError(f"Unknown billing interval {interval!r}")
    return value


def interval_months(interval: Optional[str]) -> int:
    return INTERVAL_MONTHS[normalise_interval(interval)]


def advance_months(
    value: datetime, months: int = 1, anchor_day: Optional[int] = None
) -> datetime:
    """Advance by whole calendar months, clamping to the month end but keeping the anchor."""
    step = int(months or 1)
    if step < 1:
        raise ValueError("A billing interval must be at least one month")
    anchor = int(anchor_day or value.day)
    index = value.month - 1 + step
    year = value.year + index // 12
    month = index % 12 + 1
    day = min(anchor, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)
