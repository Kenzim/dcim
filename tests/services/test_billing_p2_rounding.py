"""Billing phase 2 rounding rules (docs/billing-phase2-design.md appendix B), with property tests."""

from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services.billing.rounding import (
    TAX_ROUNDING_RULES,
    late_fee_percentage,
    round_down,
    round_half_up,
    rounding_rule,
    tax_exclusive,
    tax_floor,
    tax_inclusive,
    tax_invoice_rate,
)

money = st.integers(min_value=0, max_value=10**11)
rate_ppm = st.integers(min_value=0, max_value=1_000_000)
rate_bps = st.integers(min_value=0, max_value=10_000)
rule_names = st.sampled_from(sorted(TAX_ROUNDING_RULES))


def test_round_half_up_examples():
    assert [round_half_up(n, 2) for n in (1, 3, 5, -1, -3, -5)] == [1, 2, 3, -1, -2, -3]
    assert round_half_up(7, 3) == 2
    assert round_half_up(8, 3) == 3
    assert round_half_up(0, 5) == 0
    with pytest.raises(ValueError):
        round_half_up(1, 0)
    with pytest.raises(ValueError):
        round_half_up(1, -2)
    with pytest.raises(TypeError):
        round_half_up(1.5, 2)


def test_round_down_examples():
    assert round_down(7, 2) == 3
    assert round_down(0, 3) == 0
    with pytest.raises(ValueError):
        round_down(-1, 2)
    with pytest.raises(ValueError):
        round_down(1, 0)
    with pytest.raises(TypeError):
        round_down(True, 2)


@given(st.integers(min_value=-(10**15), max_value=10**15), st.integers(min_value=1, max_value=10**9))
def test_round_half_up_is_nearest_with_ties_away_from_zero(numerator, denominator):
    exact = Fraction(numerator, denominator)
    result = round_half_up(numerator, denominator)
    assert abs(Fraction(result) - exact) <= Fraction(1, 2)
    if abs(Fraction(result) - exact) == Fraction(1, 2):
        assert abs(result) > abs(exact)


@given(st.integers(min_value=0, max_value=10**15), st.integers(min_value=1, max_value=10**9))
def test_round_down_is_the_floor(numerator, denominator):
    result = round_down(numerator, denominator)
    assert result * denominator <= numerator < (result + 1) * denominator


@given(money, rate_bps)
def test_tax_floor_reproduces_the_legacy_formula(taxable, bps):
    assert tax_exclusive(taxable, bps * 100, "tax_floor") == (taxable * bps) // 10_000
    assert tax_exclusive(taxable, bps * 100, tax_floor) == (taxable * bps) // 10_000


def test_tax_exclusive_examples():
    assert tax_exclusive(1000, 200_000, "round_half_up") == 200
    assert tax_exclusive(1, 200_000, "round_half_up") == 0
    assert tax_exclusive(3, 200_000, "round_half_up") == 1  # 0.6
    assert tax_exclusive(5, 100_000, "round_half_up") == 1  # 0.5, tie away from zero
    assert tax_exclusive(5, 100_000, "round_half_even") == 0
    assert tax_exclusive(5, 100_000, "tax_floor") == 0
    assert tax_exclusive(10_000, 149_750, "round_half_up") == 1498  # 14.975%


def test_tax_inclusive_examples():
    assert tax_inclusive(1200, 200_000, "round_half_up") == 200
    assert tax_inclusive(100, 200_000, "round_half_up") == 17  # net 83.33 -> 83
    assert tax_inclusive(0, 200_000, "round_half_up") == 0
    assert tax_inclusive(999, 0, "round_half_up") == 0


@pytest.mark.parametrize("rule", ["tax_floor", "round_down"])
def test_tax_inclusive_floor_rules_floor_the_tax(rule):
    # Exact tax 101 * 0.2 / 1.2 = 16.83: the tax is 16 and the net 85.
    assert tax_inclusive(101, 200_000, rule) == 16
    assert tax_inclusive(1200, 200_000, rule) == 200
    assert tax_inclusive(1, 200_000, rule) == 0


def test_rule_names_and_refusals():
    assert rounding_rule("tax_floor") is tax_floor
    assert rounding_rule(round_half_up) is round_half_up
    with pytest.raises(ValueError):
        rounding_rule("banker")
    with pytest.raises(ValueError):
        tax_exclusive(-1, 200_000, "round_half_up")
    with pytest.raises(ValueError):
        tax_exclusive(1, -1, "round_half_up")
    with pytest.raises(TypeError):
        tax_inclusive(1.0, 200_000, "round_half_up")


@given(money, rate_ppm, rule_names)
def test_inclusive_net_plus_tax_equals_gross(gross, rate, rule):
    tax = tax_inclusive(gross, rate, rule)
    net = gross - tax
    assert 0 <= tax <= gross
    assert net + tax == gross
    # The net is the rule's rounding of the exact net, so within one unit of it.
    assert abs(Fraction(net) - Fraction(gross * 1_000_000, 1_000_000 + rate)) <= 1


@given(money, rate_ppm, rule_names)
def test_exclusive_tax_is_within_one_unit_of_the_exact_tax(base, rate, rule):
    tax = tax_exclusive(base, rate, rule)
    assert abs(Fraction(tax) - Fraction(base * rate, 1_000_000)) < 1


@given(money, money, rate_ppm, rule_names)
def test_exclusive_is_monotonic_in_base(first, second, rate, rule):
    low, high = sorted((first, second))
    assert tax_exclusive(low, rate, rule) <= tax_exclusive(high, rate, rule)


@given(money, money, rate_ppm, rule_names)
def test_inclusive_is_monotonic_in_base(first, second, rate, rule):
    low, high = sorted((first, second))
    assert tax_inclusive(low, rate, rule) <= tax_inclusive(high, rate, rule)


@given(st.lists(money, max_size=12), rate_ppm, rule_names, st.booleans())
def test_invoice_rate_spreading_sums_exactly(bases, rate, rule, inclusive):
    shares = tax_invoice_rate(bases, rate, rule, inclusive=inclusive)
    single = (tax_inclusive if inclusive else tax_exclusive)(sum(bases), rate, rule)
    assert len(shares) == len(bases)
    assert sum(shares) == single
    assert all(share >= 0 for share in shares)
    assert all(share == 0 for share, base in zip(shares, bases) if base == 0)


@given(st.lists(money, min_size=1, max_size=12), rate_bps)
def test_invoice_rate_with_tax_floor_matches_legacy_on_the_total(bases, bps):
    shares = tax_invoice_rate(bases, bps * 100, "tax_floor")
    assert sum(shares) == (sum(bases) * bps) // 10_000


def test_invoice_rate_example():
    # 3 x 0.33 at 20%: line rounding gives 0.07 x 3 = 0.21, invoice rounding 0.20.
    assert tax_invoice_rate([33, 33, 33], 200_000, "round_half_up") == [7, 7, 6]
    assert tax_invoice_rate([], 200_000, "round_half_up") == []


def test_late_fee_percentage_examples():
    assert late_fee_percentage(10_000, 150) == 150
    assert late_fee_percentage(333, 150) == 5  # 4.995 rounds half up to 5
    assert late_fee_percentage(1000, 5) == 1  # 0.5, tie away from zero
    assert late_fee_percentage(1000, 100, min_cents=500) == 500
    assert late_fee_percentage(1_000_000, 1000, max_cents=2_500) == 2_500
    assert late_fee_percentage(1_000_000, 1000, max_cents=0) == 100_000
    with pytest.raises(ValueError):
        late_fee_percentage(-1, 100)


@given(
    money,
    st.integers(min_value=0, max_value=10_000),
    st.integers(min_value=0, max_value=10**9),
    st.integers(min_value=0, max_value=10**9),
)
def test_late_fee_stays_within_bounds(remaining, bp, minimum, maximum):
    fee = late_fee_percentage(remaining, bp, min_cents=minimum, max_cents=maximum)
    if maximum:
        assert fee <= maximum
    if not maximum or minimum <= maximum:
        assert fee >= minimum
    unclamped = round_half_up(remaining * bp, 10_000)
    if minimum <= unclamped and (not maximum or unclamped <= maximum):
        assert fee == unclamped
