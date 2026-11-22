"""Named rounding rules (app/services/billing/rounding.py), with property tests."""

from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services.billing.rounding import (
    refund_tax_proportional,
    round_half_even,
    tax_split_largest_remainder,
)

money = st.integers(min_value=0, max_value=10**9)


def test_round_half_even_examples():
    assert [round_half_even(n, 2) for n in (1, 3, 5, -1, -3)] == [0, 2, 2, 0, -2]
    assert round_half_even(7, 3) == 2
    assert round_half_even(8, 3) == 3
    assert round_half_even(5, -2) == -2
    with pytest.raises(ZeroDivisionError):
        round_half_even(1, 0)
    with pytest.raises(TypeError):
        round_half_even(1.5, 2)


@given(st.integers(-(10**12), 10**12), st.integers(1, 10**6))
def test_round_half_even_is_nearest(numerator, denominator):
    result = round_half_even(numerator, denominator)
    exact = Fraction(numerator, denominator)
    assert abs(exact - result) <= Fraction(1, 2)
    if abs(exact - result) == Fraction(1, 2):
        assert result % 2 == 0


def test_tax_split_examples():
    assert tax_split_largest_remainder(10, [1, 1, 1]) == [4, 3, 3]
    assert tax_split_largest_remainder(0, [5, 0]) == [0, 0]
    assert tax_split_largest_remainder(0, []) == []
    assert tax_split_largest_remainder(7, [0, 3]) == [0, 7]
    with pytest.raises(ValueError):
        tax_split_largest_remainder(1, [0, 0])
    with pytest.raises(ValueError):
        tax_split_largest_remainder(-1, [1])
    with pytest.raises(ValueError):
        tax_split_largest_remainder(1, [1, -1])
    with pytest.raises(TypeError):
        tax_split_largest_remainder(1, [1.0])


@given(money, st.lists(st.integers(0, 10**7), min_size=1, max_size=20))
def test_tax_split_sums_and_stays_within_one_unit(total, weights):
    if sum(weights) == 0:
        weights = weights[:-1] + [1]
    shares = tax_split_largest_remainder(total, weights)
    assert sum(shares) == total
    assert all(share >= 0 for share in shares)
    weight_sum = sum(weights)
    for share, weight in zip(shares, weights):
        exact = Fraction(total * weight, weight_sum)
        assert abs(share - exact) < 1
        if weight == 0:
            assert share == 0


@given(money, st.lists(st.integers(1, 1000), min_size=2, max_size=8))
def test_tax_split_is_deterministic_with_ties_to_earlier_lines(total, weights):
    assert tax_split_largest_remainder(total, weights) == tax_split_largest_remainder(total, list(weights))
    equal = tax_split_largest_remainder(total, [1] * len(weights))
    assert equal == sorted(equal, reverse=True)


def test_refund_tax_examples():
    assert refund_tax_proportional(1200, 200, 1200) == 200
    assert refund_tax_proportional(600, 200, 1200) == 100
    assert refund_tax_proportional(0, 200, 1200) == 0
    assert refund_tax_proportional(0, 0, 0) == 0
    with pytest.raises(ValueError):
        refund_tax_proportional(1300, 200, 1200)
    with pytest.raises(ValueError):
        refund_tax_proportional(-1, 200, 1200)
    with pytest.raises(ValueError):
        refund_tax_proportional(100, 1300, 1200)


@st.composite
def invoice_and_refunds(draw):
    total = draw(st.integers(1, 10**8))
    tax = draw(st.integers(0, total))
    parts = draw(st.lists(st.integers(1, total), min_size=1, max_size=6))
    refunds, left = [], total
    for part in parts:
        if left == 0:
            break
        amount = min(part, left)
        refunds.append(amount)
        left -= amount
    return total, tax, refunds


def test_per_part_rounding_can_overshoot_so_refunds_use_cumulative_amounts():
    # Four refunds of 1 against tax 3 on 4: each rounds to 1, four in all.
    assert sum(refund_tax_proportional(1, 3, 4) for _ in range(4)) == 4
    cumulative = [refund_tax_proportional(done, 3, 4) for done in range(5)]
    assert sum(after - before for before, after in zip(cumulative, cumulative[1:])) == 3


@given(invoice_and_refunds())
def test_cumulative_refund_tax_never_exceeds_invoice_tax(case):
    total, tax, refunds = case
    refunded = taxed = 0
    for amount in refunds:
        part = refund_tax_proportional(refunded + amount, tax, total) - taxed
        assert 0 <= part <= amount
        refunded += amount
        taxed += part
        assert taxed <= tax
    if refunded == total:
        assert taxed == tax


@given(invoice_and_refunds())
def test_refund_tax_never_exceeds_invoice_tax_and_full_refund_is_exact(case):
    total, tax, refunds = case
    assert refund_tax_proportional(total, tax, total) == tax
    for amount in refunds:
        part = refund_tax_proportional(amount, tax, total)
        assert 0 <= part <= tax
        assert 0 <= amount - part
    # Each refund on its own never takes more tax than its exact share rounds to.
    assert all(
        abs(refund_tax_proportional(amount, tax, total) - Fraction(amount * tax, total)) <= Fraction(1, 2)
        for amount in refunds
    )
