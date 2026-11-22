"""The operator currency: the one currency Rackflow bills in (docs/billing-phase2-design.md 4.10).

It is ``settings.commerce_default_currency`` for the whole system; billing
code reads it here instead of naming a currency.
"""

from __future__ import annotations


def operator_currency() -> str:
    """The operator currency as an upper case ISO 4217 code."""
    from app.core.config import settings

    return str(settings.commerce_default_currency or "").strip().upper()
