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
}
DEFAULT_INTERVAL = "monthly"


def normalise_interval(interval: Optional[str]) -> str:
    value = (interval or DEFAULT_INTERVAL).strip().lower()
    return value if value in INTERVAL_MONTHS else DEFAULT_INTERVAL


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
