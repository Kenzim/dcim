"""Named rounding rules for money (docs/billing-phase1-design.md appendix B and
docs/billing-phase2-design.md appendix B).

Every amount computed from other amounts uses one of these. Each is a pure
function on integers (minor units); none uses floats.
"""

from __future__ import annotations

from typing import Callable, Sequence, Union

# A primitive rule: ``rule(numerator, denominator) -> int``.
RoundingRule = Callable[[int, int], int]

# Tax rates are integer parts per million of the base (20% is 200_000).
PPM = 1_000_000


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


# ------------------------------------------------------------------ billing phase 2


def round_half_up(numerator: int, denominator: int) -> int:
    """``numerator / denominator`` rounded to the nearest integer, ties away from zero.

    ``denominator`` must be positive.
    """
    _require_int("numerator", numerator)
    _require_int("denominator", denominator)
    if denominator <= 0:
        raise ValueError("round_half_up needs a positive denominator")
    magnitude, remainder = divmod(abs(numerator), denominator)
    if 2 * remainder >= denominator:
        magnitude += 1
    return magnitude if numerator >= 0 else -magnitude


def round_down(numerator: int, denominator: int) -> int:
    """``numerator / denominator`` rounded down; ``numerator`` must not be negative."""
    _require_int("numerator", numerator)
    _require_int("denominator", denominator)
    if denominator <= 0:
        raise ValueError("round_down needs a positive denominator")
    if numerator < 0:
        raise ValueError("round_down needs a non-negative numerator")
    return numerator // denominator


def tax_floor(numerator: int, denominator: int) -> int:
    """``round_down``, named for the rules mirrored from ``tax_rates``.

    With ``rate_ppm = rate_bps * 100`` it reproduces the legacy
    ``(taxable * rate_bps) // 10_000`` exactly.
    """
    return round_down(numerator, denominator)


# Rule names a tax rule's ``rounding_rule`` may hold.
TAX_ROUNDING_RULES: dict[str, RoundingRule] = {
    "tax_floor": tax_floor,
    "round_down": round_down,
    "round_half_up": round_half_up,
    "round_half_even": round_half_even,
}


def rounding_rule(rule: Union[str, RoundingRule]) -> RoundingRule:
    """The primitive for a rule name of ``TAX_ROUNDING_RULES`` (or the callable itself)."""
    if callable(rule):
        return rule
    try:
        return TAX_ROUNDING_RULES[rule]
    except KeyError:
        raise ValueError(f"unknown rounding rule {rule!r}") from None


def _require_rate(rate_ppm: int) -> None:
    _require_int("rate_ppm", rate_ppm)
    if rate_ppm < 0:
        raise ValueError("rate_ppm must not be negative")


def _require_amount(name: str, value: int) -> None:
    _require_int(name, value)
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def tax_exclusive(base: int, rate_ppm: int, rule: Union[str, RoundingRule]) -> int:
    """Tax added to the net amount ``base``: ``rule(base * rate_ppm, 1_000_000)``."""
    _require_amount("base", base)
    _require_rate(rate_ppm)
    return rounding_rule(rule)(base * rate_ppm, PPM)


# Rules that floor the tax in both modes.
FLOOR_RULES: frozenset[RoundingRule] = frozenset({tax_floor, round_down})


def tax_inclusive(gross: int, rate_ppm: int, rule: Union[str, RoundingRule]) -> int:
    """Tax inside the gross amount ``gross``; the net is ``gross`` minus it.

    A floor rule (``FLOOR_RULES``) floors the exact tax:
    ``rule(gross * rate_ppm, 1_000_000 + rate_ppm)``. Any other rule rounds the
    net, and the tax is the rest: ``gross - rule(gross * 1_000_000, 1_000_000 + rate_ppm)``.
    """
    _require_amount("gross", gross)
    _require_rate(rate_ppm)
    primitive = rounding_rule(rule)
    if primitive in FLOOR_RULES:
        return primitive(gross * rate_ppm, PPM + rate_ppm)
    return gross - primitive(gross * PPM, PPM + rate_ppm)


def tax_invoice_rate(
    bases: Sequence[int],
    rate_ppm: int,
    rule: Union[str, RoundingRule],
    *,
    inclusive: bool = False,
) -> list[int]:
    """Tax of each line for the rounding point ``invoice_rate``.

    The tax on the sum of ``bases`` is rounded once (``tax_exclusive``, or
    ``tax_inclusive`` when the bases are gross amounts) and spread over the
    lines with ``tax_split_largest_remainder`` weighted by base, so the line
    taxes sum exactly to the rounded total.
    """
    for base in bases:
        _require_amount("base", base)
    total = sum(bases)
    total_tax = (tax_inclusive if inclusive else tax_exclusive)(total, rate_ppm, rule)
    return tax_split_largest_remainder(total_tax, list(bases))


def late_fee_percentage(remaining: int, bp: int, *, min_cents: int = 0, max_cents: int = 0) -> int:
    """Percentage late fee: ``round_half_up(remaining * bp, 10_000)`` clamped to the bounds.

    ``bp`` is basis points of ``remaining`` (100 is 1%). The fee is raised to
    ``min_cents`` and lowered to ``max_cents``; ``max_cents`` 0 means no maximum.
    """
    for name, value in (("remaining", remaining), ("bp", bp), ("min_cents", min_cents), ("max_cents", max_cents)):
        _require_amount(name, value)
    fee = round_half_up(remaining * bp, 10_000)
    fee = max(fee, min_cents)
    if max_cents:
        fee = min(fee, max_cents)
    return fee
