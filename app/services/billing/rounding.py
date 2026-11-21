"""Named rounding rules for money (docs/billing-phase1-design.md appendix B).

Every amount computed from other amounts uses one of these. Each is a pure
function on integers (minor units); none uses floats.
"""

from __future__ import annotations

from typing import Sequence


def _require_int(name: str, value: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    return value


def round_half_even(numerator: int, denominator: int) -> int:
    """``numerator / denominator`` rounded to the nearest integer, ties to the even quotient."""
    _require_int("numerator", numerator)
    _require_int("denominator", denominator)
    if denominator == 0:
        raise ZeroDivisionError("round_half_even with a zero denominator")
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    quotient, remainder = divmod(numerator, denominator)
    twice = 2 * remainder
    if twice > denominator or (twice == denominator and quotient % 2 == 1):
        quotient += 1
    return quotient


def tax_split_largest_remainder(total_tax: int, weights: Sequence[int]) -> list[int]:
    """Spread ``total_tax`` over lines in proportion to ``weights``.

    Each share is ``floor(total_tax * w / sum(weights))``; the units left over
    go one each to the lines with the largest fractional remainders, ties to the
    earlier line. The shares sum exactly to ``total_tax``. ``total_tax`` and every
    weight must be non-negative; with all weights 0 (or no lines) only a zero
    tax can be split.
    """
    _require_int("total_tax", total_tax)
    if total_tax < 0:
        raise ValueError("total_tax must not be negative")
    for weight in weights:
        _require_int("weight", weight)
        if weight < 0:
            raise ValueError("weights must not be negative")
    weight_sum = sum(weights)
    if weight_sum == 0:
        if total_tax:
            raise ValueError("cannot split a non-zero tax over zero weights")
        return [0] * len(weights)
    shares: list[int] = []
    remainders: list[tuple[int, int]] = []
    for index, weight in enumerate(weights):
        share, remainder = divmod(total_tax * weight, weight_sum)
        shares.append(share)
        remainders.append((remainder, index))
    left = total_tax - sum(shares)
    # Largest remainder first; equal remainders keep line order.
    for _remainder, index in sorted(remainders, key=lambda item: (-item[0], item[1]))[:left]:
        shares[index] += 1
    return shares


def refund_tax_proportional(amount: int, invoice_tax: int, invoice_total: int) -> int:
    """Tax part of a refund of ``amount`` against an invoice of ``invoice_total`` with ``invoice_tax``.

    ``round_half_even(amount * invoice_tax / invoice_total)``, 0 when the
    invoice total is 0. The net part is ``amount - tax``. All three must be
    non-negative, with ``amount <= invoice_total`` and ``invoice_tax <= invoice_total``.
    """
    for name, value in (
        ("amount", amount),
        ("invoice_tax", invoice_tax),
        ("invoice_total", invoice_total),
    ):
        _require_int(name, value)
        if value < 0:
            raise ValueError(f"{name} must not be negative")
    if invoice_total == 0:
        return 0
    if amount > invoice_total:
        raise ValueError("amount must not exceed invoice_total")
    if invoice_tax > invoice_total:
        raise ValueError("invoice_tax must not exceed invoice_total")
    return round_half_even(amount * invoice_tax, invoice_total)
