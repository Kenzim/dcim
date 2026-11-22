"""TaxEngine.calculate: selection, place of supply, reverse charge, amounts (design 4.3, 4.4, 15.2)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.models.tax import TaxExemption
from app.services.billing.rounding import tax_exclusive, tax_inclusive
from app.services.tax.engine import SCOPE_OUT, TaxEngine
from app.services.tax.rules_repo import TaxRateError, rate_ppm
from app.services.settings_store import set_setting
from tests.services.tax_helpers import (
    NOW,
    add_registration,
    add_rule,
    buyer,
    context,
    enable_tax,
    line,
    retail_account,
    seed_classes,
    valid_business,
)


@pytest.fixture(autouse=True)
def _admin_rules_apply(db_session):
    """Tax enabled with no seller country: every ``ANY`` rule applies (4.3 step 2)."""
    enable_tax(db_session, "")


def _calc(db, lines, **kwargs):
    return TaxEngine.calculate(db, context(lines, **kwargs))


# --- rates ---


def test_rate_ppm_is_exact_and_refuses_more_than_four_decimals():
    assert rate_ppm("20") == 200_000
    assert rate_ppm(Decimal("14.975")) == 149_750
    assert rate_ppm("0.0001") == 1
    with pytest.raises(TaxRateError):
        rate_ppm("1.00001")
    with pytest.raises(TaxRateError):
        rate_ppm("-1")
    with pytest.raises(TaxRateError):
        rate_ppm("100.0001")


# --- selection order (4.3 step 4) ---


def test_most_specific_rule_wins_then_priority_then_lowest_id(db_session):
    seed_classes(db_session)
    add_rule(db_session, name="none", country=None, rate="1", source="admin")
    add_rule(db_session, name="country", rate="2")
    add_rule(db_session, name="region", region="Wales", rate="3")
    add_rule(db_session, name="postcode", rate="4", postcode_pattern=r"CF\d+.*")
    facts = buyer("GB", region="Wales", postcode="cf10 1aa")
    assert _calc(db_session, [line("a", 1000)], facts=facts).lines["a"].parts[0].name == "postcode"
    facts = buyer("GB", region="Wales", postcode="SW11 5PX")
    assert _calc(db_session, [line("a", 1000)], facts=facts).lines["a"].parts[0].name == "region"
    assert _calc(db_session, [line("a", 1000)], facts=buyer("GB")).lines["a"].parts[0].name == "country"
    assert _calc(db_session, [line("a", 1000)], facts=buyer("US")).lines["a"].parts[0].name == "none"


def test_priority_then_lowest_id_break_ties(db_session):
    add_rule(db_session, name="first", rate="2")
    add_rule(db_session, name="second", rate="3")
    assert _calc(db_session, [line("a", 1000)]).lines["a"].parts[0].name == "first"
    add_rule(db_session, name="urgent", rate="4", priority=5)
    assert _calc(db_session, [line("a", 1000)]).lines["a"].parts[0].name == "urgent"


def test_class_customer_type_validity_and_enabled_filter_rules(db_session):
    add_rule(db_session, name="reduced", rate="5", tax_class="reduced")
    add_rule(db_session, name="business only", rate="7", customer_type="business")
    add_rule(db_session, name="disabled", rate="9", enabled=False)
    add_rule(db_session, name="future", rate="11", valid_from=NOW + timedelta(days=1))
    add_rule(db_session, name="expired", rate="13", valid_to=NOW)
    result = _calc(db_session, [line("a", 1000), line("b", 1000, tax_class="reduced")])
    assert result.lines["a"].parts == ()
    assert [p.name for p in result.lines["b"].parts] == ["reduced"]
    business = _calc(db_session, [line("a", 1000)], facts=buyer(business=True))
    assert [p.name for p in business.lines["a"].parts] == ["business only"]


def test_no_matching_rule_means_no_tax(db_session):
    seed_classes(db_session)
    result = _calc(db_session, [line("a", 1000)])
    assert result.tax_cents == 0 and result.lines["a"].parts == ()


# --- amounts (4.4) ---


def test_line_rounding_rounds_each_line(db_session):
    add_rule(db_session, rate="20", rounding_point="line", rounding_rule="round_half_up")
    result = _calc(db_session, [line("a", 3), line("b", 3), line("c", 3)])
    # 0.6 each rounds up to 1.
    assert [result.lines[k].tax_cents for k in "abc"] == [1, 1, 1]


def test_invoice_rate_rounds_the_sum_once_and_spreads_it(db_session):
    add_rule(db_session, rate="20", rounding_point="invoice_rate", rounding_rule="round_half_up")
    result = _calc(db_session, [line("a", 3), line("b", 3), line("c", 3)])
    # 9 * 20% = 1.8 rounds to 2, spread over three equal lines.
    assert sum(result.lines[k].tax_cents for k in "abc") == 2
    assert result.summary[0].tax_cents == 2 and result.summary[0].base_cents == 9


def test_inclusive_prices_net_plus_tax_equals_gross(db_session):
    add_rule(db_session, rate="20", rounding_point="line", rounding_rule="round_half_up")
    result = _calc(db_session, [line("a", 1001)], inclusive=True)
    part = result.lines["a"].parts[0]
    assert part.tax_cents == tax_inclusive(1001, 200_000, "round_half_up")
    assert part.base_cents + part.tax_cents == 1001


def test_inclusive_invoice_rate_adds_up_to_the_gross(db_session):
    add_rule(db_session, rate="20", rounding_point="invoice_rate", rounding_rule="round_half_up")
    result = _calc(db_session, [line("a", 1001), line("b", 999)], inclusive=True)
    total_tax = sum(p.tax_cents for k in "ab" for p in result.lines[k].parts)
    assert total_tax == tax_inclusive(2000, 200_000, "round_half_up")
    assert sum(p.base_cents + p.tax_cents for k in "ab" for p in result.lines[k].parts) == 2000


def test_compound_rule_taxes_base_plus_earlier_taxes(db_session):
    add_rule(db_session, name="federal", rate="5", country="CA", rounding_point="line")
    add_rule(
        db_session, name="provincial", rate="9.975", country="CA", rounding_point="line",
        compound=True, compound_order=1,
    )
    result = _calc(db_session, [line("a", 10_000)], facts=buyer("CA"))
    federal, provincial = result.lines["a"].parts
    assert federal.tax_cents == 500
    assert provincial.base_cents == 10_500
    assert provincial.tax_cents == tax_exclusive(10_500, 99_750, "round_half_up")
    assert result.lines["a"].tax_cents == federal.tax_cents + provincial.tax_cents


def test_inclusive_with_two_taxes_adds_up_to_the_gross(db_session):
    add_rule(db_session, name="federal", rate="5", country="CA", rounding_point="line")
    add_rule(db_session, name="provincial", rate="9.975", country="CA", rounding_point="line", compound=True)
    result = _calc(db_session, [line("a", 11_549)], facts=buyer("CA"), inclusive=True)
    parts = result.lines["a"].parts
    assert parts[0].base_cents + sum(p.tax_cents for p in parts) == 11_549


def test_inclusive_with_three_taxes_adds_up_to_the_gross(db_session):
    for name, rate in (("a", "5"), ("b", "7"), ("c", "3")):
        add_rule(db_session, name=name, rate=rate, country="CA", rounding_point="line")
        db_session.flush()
    # Three plain taxes of the same specificity: only the first applies; make b and c compound.
    from app.models.tax import TaxRule

    for order, name in enumerate(("b", "c"), start=1):
        rule = db_session.query(TaxRule).filter_by(name=name).one()
        rule.compound, rule.compound_order = True, order
    db_session.flush()
    for gross in (1, 2, 3, 99, 101, 1_000):
        parts = _calc(db_session, [line("a", gross)], facts=buyer("CA"), inclusive=True).lines["a"].parts
        assert len(parts) == 3
        assert parts[0].base_cents + sum(p.tax_cents for p in parts) == gross


INCLUSIVE_COMBINATIONS = [
    # (rate_percent, compound, reverse_charge) per part, in compound order.
    [("5", False, False), ("7", False, False), ("3", False, False)],
    [("5", False, False), ("9.975", True, False)],
    [("5", False, False), ("7", False, False), ("9.975", True, False)],
    [("20", False, False), ("0", False, True), ("5", True, False)],
    [("1", False, False), ("1", False, False), ("1", False, False), ("1", True, False)],
    [("33.3333", False, False), ("33.3333", False, False), ("33.3333", True, False)],
    [("0", False, True), ("0", False, True)],
]


@pytest.mark.parametrize("rule", ["round_half_up", "tax_floor", "round_half_even"])
@pytest.mark.parametrize("rates", INCLUSIVE_COMBINATIONS)
def test_inclusive_multi_always_adds_up_to_the_gross(rates, rule):
    from app.services.tax.engine import _inclusive_multi, _Part

    for gross in range(0, 501):
        parts = [
            _Part(
                rule=None, name=f"t{i}", rate_percent=Decimal(rate), rate_ppm=0 if rc else rate_ppm(rate),
                jurisdiction="ANY", reverse_charge=rc, legal_note_key=None, rounding_point="line",
                rounding_rule=rule, compound=compound, source="rules",
            )
            for i, (rate, compound, rc) in enumerate(rates)
        ]
        _inclusive_multi(parts, gross)
        net = parts[0].base
        assert net + sum(p.tax for p in parts) == gross, (gross, [(p.base, p.tax) for p in parts])
        assert all(p.tax >= 0 for p in parts) and net >= 0
        assert all(p.tax == 0 for p in parts if p.reverse_charge)
        for index, part in enumerate(parts):
            if part.compound:
                assert part.base == net + sum(p.tax for p in parts[:index])
            else:
                assert part.base == net


def test_discount_reduces_each_line_base_by_its_share(db_session):
    add_rule(db_session, rate="20", rounding_point="line", rounding_rule="round_half_up")
    result = _calc(
        db_session,
        [line("a", 3000), line("b", 1000), line("d", -1000, kind="discount")],
    )
    assert result.lines["a"].parts[0].base_cents == 2250
    assert result.lines["b"].parts[0].base_cents == 750
    assert result.lines["d"].parts == ()
    assert result.tax_cents == 600


def test_legacy_mirror_rule_reproduces_floor_division(db_session):
    add_rule(db_session, rate="17.5", source="tax_rates", rounding_rule="tax_floor")
    for taxable in (1, 7, 99, 101, 12_345, 99_999):
        result = _calc(db_session, [line("a", taxable)])
        assert result.tax_cents == (taxable * 1750) // 10_000


# --- scope (4.6) ---


def test_credit_topup_is_out_of_scope(db_session):
    add_rule(db_session)
    result = _calc(db_session, [line("a", 1000)], purpose="credit_topup")
    assert result.scope == SCOPE_OUT and result.tax_cents == 0


def test_renewal_and_reseller_settings_decide_scope(db_session):
    add_rule(db_session)
    assert _calc(db_session, [line("a", 1000)], purpose="cycle_charge").tax_cents == 200
    assert _calc(db_session, [line("a", 1000)], purpose="cycle_charge", owner="reseller").scope == SCOPE_OUT
    set_setting(db_session, "billing_tax_resellers_enabled", True)
    assert _calc(db_session, [line("a", 1000)], purpose="deploy_charge", owner="reseller").tax_cents == 200
    set_setting(db_session, "billing_tax_retail_renewals", False)
    assert _calc(db_session, [line("a", 1000)], purpose="cycle_charge").scope == SCOPE_OUT


# --- tax disabled: only mirrored rules (4.3 step 1) ---


def test_disabled_tax_considers_only_mirrored_any_rules(db_session):
    set_setting(db_session, "billing_tax_enabled", False)
    add_rule(db_session, name="mirror", rate="20", source="tax_rates", rounding_rule="tax_floor")
    add_rule(db_session, name="pack", rate="25", source="pack", jurisdiction="GB", priority=50)
    result = _calc(db_session, [line("a", 1000)])
    assert [p.name for p in result.lines["a"].parts] == ["mirror"]
    assert result.lines["a"].parts[0].source == "tax_rates"


def test_mirrored_region_row_matches_a_buyer_with_no_region_as_tax_rates_did(db_session):
    add_rule(db_session, name="CA", rate="7.25", country="US", region="CA", source="tax_rates", rounding_rule="tax_floor")
    result = _calc(db_session, [line("a", 10_000)], facts=buyer("US"))
    assert result.tax_cents == 725


# --- exemptions (4.3 step 3) ---


def test_exempt_account_gets_no_tax_and_the_exempt_note(db_session):
    add_rule(db_session)
    result = _calc(db_session, [line("a", 1000)], facts=buyer(exempt_all=True, exempt_reason="charity"))
    assert result.tax_cents == 0
    assert result.legal_note_keys == ("exempt_customer",)
    assert result.evidence.values["legal_note_values"]["exempt_customer"] == {"reason": "charity"}


def test_class_exemption_covers_only_its_classes(db_session):
    classes = seed_classes(db_session)
    add_rule(db_session)
    add_rule(db_session, name="reduced", rate="5", tax_class="reduced")
    _user, account = retail_account(db_session)
    exemption = TaxExemption(
        scope="account", billing_account_id=account.id, reason="certificate", valid_from=NOW - timedelta(days=1),
        tax_class_ids=[classes["reduced"]],
    )
    db_session.add(exemption)
    db_session.flush()
    result = _calc(
        db_session,
        [line("a", 1000), line("b", 1000, tax_class="reduced")],
        facts=buyer(exemption_ids=(exemption.id,)),
    )
    assert result.lines["a"].tax_cents == 200
    assert result.lines["b"].parts == ()
    assert f"exemption:{exemption.id}" in result.evidence.values["explained_by"]


# --- place of supply and reverse charge (4.3 steps 2 and 5) ---


def _uk_seller(db):
    enable_tax(db, "GB")
    add_registration(db, "GB")
    add_rule(db, name="UK VAT 20%", jurisdiction="GB", source="pack", tax_class="standard")
    add_rule(
        db, name="Reverse charge", rate="0", country=None, jurisdiction="GB", source="pack",
        customer_type="business", reverse_charge=True, reverse_charge_zone="EU",
        legal_note_key="gb_outside_scope_b2b",
    )


def test_uk_seller_uk_buyer_is_charged_uk_vat(db_session):
    _uk_seller(db_session)
    result = _calc(db_session, [line("a", 1000)], facts=buyer("GB"))
    assert result.tax_cents == 200 and result.registration_id is not None


def test_uk_seller_eu_business_with_fresh_valid_number_is_reverse_charged(db_session):
    _uk_seller(db_session)
    result = _calc(db_session, [line("a", 1000)], facts=valid_business("DE"))
    part = result.lines["a"].parts[0]
    assert part.reverse_charge and part.tax_cents == 0 and part.base_cents == 1000
    assert result.legal_note_keys == ("gb_outside_scope_b2b",)


@pytest.mark.parametrize(
    "facts,reason",
    [
        (buyer("DE", business=True, tax_id="1", status="unchecked"), "tax_id_unchecked"),
        (buyer("DE", business=True, tax_id="1", status="invalid"), "tax_id_invalid"),
        (buyer("DE", business=True, tax_id="1", status="unavailable"), "tax_id_unavailable"),
        (valid_business("DE", days_old=31), "tax_id_stale"),
        (buyer("DE", business=True, status="valid", checked_at=NOW), "tax_id_missing"),
    ],
)
def test_reverse_charge_needs_each_condition(db_session, facts, reason):
    _uk_seller(db_session)
    result = _calc(db_session, [line("a", 1000)], facts=facts)
    assert not any(p.reverse_charge for p in result.lines["a"].parts)
    assert reason in result.evidence.values["reasons"]
    # No OSS registration: an EU consumer case with no tax and the not-registered evidence.
    assert result.evidence.values.get("notes") == ["not_registered"]


def test_consumer_is_not_reverse_charged(db_session):
    _uk_seller(db_session)
    result = _calc(db_session, [line("a", 1000)], facts=buyer("DE"))
    assert result.tax_cents == 0 and result.evidence.values["notes"] == ["not_registered"]


def test_uk_seller_rest_of_world_is_outside_scope(db_session):
    _uk_seller(db_session)
    result = _calc(db_session, [line("a", 1000)], facts=buyer("US"))
    assert result.tax_cents == 0 and result.legal_note_keys == ("outside_scope",)
    business = _calc(db_session, [line("a", 1000)], facts=valid_business("US"))
    assert business.legal_note_keys == ("outside_scope",)


def test_eu_consumer_with_oss_registration_gets_the_member_state_rate(db_session):
    _uk_seller(db_session)
    add_registration(db_session, "EU_OSS", country="IE", scheme="oss_non_union")
    add_rule(db_session, name="DE VAT 19%", rate="19", country="DE", jurisdiction="EU_OSS", source="pack")
    result = _calc(db_session, [line("a", 1000)], facts=buyer("DE"))
    assert result.tax_cents == 190


def test_eu_seller_other_state_consumer_without_oss_pays_the_seller_state_rate(db_session):
    enable_tax(db_session, "IE")
    add_registration(db_session, "IE")
    add_rule(db_session, name="IE VAT 23%", rate="23", country="IE", jurisdiction="IE", source="pack")
    result = _calc(db_session, [line("a", 1000)], facts=buyer("FR"))
    assert result.tax_cents == 230


def test_eu_seller_eu_business_reverse_charge_without_a_pack_rule_has_the_eu_note(db_session):
    enable_tax(db_session, "IE")
    add_registration(db_session, "IE")
    result = _calc(db_session, [line("a", 1000)], facts=valid_business("FR"))
    assert result.tax_cents == 0 and result.legal_note_keys == ("eu_reverse_charge",)


@pytest.mark.parametrize(
    "seller,buyer_country,note,name",
    [
        ("IE", "FR", "eu_reverse_charge", "Reverse charge"),
        ("GB", "DE", "gb_outside_scope_b2b", "Outside the scope of UK VAT"),
        ("GB", "US", "outside_scope", "Outside the scope of VAT"),
    ],
)
def test_reverse_charge_without_a_rule_gets_a_zero_tax_reverse_charge_part(db_session, seller, buyer_country, note, name):
    enable_tax(db_session, seller)
    registration = add_registration(db_session, seller)
    add_rule(db_session, name="ANY 10%", rate="10", country=buyer_country, source="admin")
    result = _calc(
        db_session,
        [line("a", 1000), line("b", 500, tax_class="reduced"), line("c", 300, tax_class="outside_scope"),
         line("d", -100, kind="discount")],
        facts=valid_business(buyer_country),
    )
    # The discount of 100 spreads over a, b and c by largest remainder: 55, 28 and 17.
    for key, base in (("a", 945), ("b", 472)):
        (part,) = result.lines[key].parts
        assert (part.name, part.reverse_charge, part.tax_cents, part.base_cents) == (name, True, 0, base)
        assert (part.rule_id, part.jurisdiction, part.legal_note_key) == (None, seller, note)
    assert result.lines["c"].parts == () and result.lines["d"].parts == ()
    assert result.tax_cents == 0 and result.legal_note_keys == (note,)
    assert result.registration_id == registration.id
    assert result.evidence.values["explained_by"] == ["reverse_charge"]


def test_no_seller_country_only_any_rules_apply(db_session):
    enable_tax(db_session, "")
    add_rule(db_session, name="pack", jurisdiction="GB", source="pack")
    add_rule(db_session, name="admin any", rate="10", source="admin")
    result = _calc(db_session, [line("a", 1000)])
    assert [p.name for p in result.lines["a"].parts] == ["admin any"]


# --- evidence and hash ---


def test_ip_country_conflict_is_recorded(db_session):
    seed_classes(db_session)
    result = _calc(db_session, [line("a", 1000)], evidence={"ip_country": "FR"})
    assert result.evidence.values["conflict"] is True


def test_context_hash_changes_with_rules_and_is_stable_otherwise(db_session):
    add_rule(db_session)
    first = _calc(db_session, [line("a", 1000)])
    assert _calc(db_session, [line("a", 1000)]).context_hash == first.context_hash
    assert _calc(db_session, [line("a", 1000)], at=NOW + timedelta(hours=1)).context_hash == first.context_hash
    add_rule(db_session, name="other", rate="5")
    assert _calc(db_session, [line("a", 1000)]).context_hash != first.context_hash
