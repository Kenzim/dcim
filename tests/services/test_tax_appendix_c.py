"""Appendix C golden cases through TaxEngine.calculate (docs/billing-phase2-design.md 4.3 and appendix C).

Each case applies the built-in packs, sets the seller's establishment country
and registrations, and checks the tax per line, the reverse charge flag, the
legal notes and the registration the engine chose. A case with no tax still
names the establishment registration: the invoice is issued under it and
prints its number (design 4.3 step 2.2; for the no-tax cases of step 2.3 this
is the engine's choice, pending the design saying so).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.models.tax import SellerTaxRegistration, TaxIdCheck
from app.plugin_host import providers as plugin_providers
from app.services.settings_store import set_setting
from app.services.tax.context import BuyerTaxFacts, TaxContext, TaxLineInput
from app.services.tax.engine import TaxEngine
from app.services.tax.packs import registry as registry_module
from tests.services.tax_pack_helpers import apply_pack, seed_tax_basics

TAX_POINT = datetime(2027, 1, 15, 10, 0, tzinfo=timezone.utc)
FRESH = TAX_POINT - timedelta(days=5)
STALE = TAX_POINT - timedelta(days=45)

# One digital service line of 100.00 and one standard line of 19.99.
LINES = (
    TaxLineInput(key="line:0", net_cents=10_000, tax_class_code="digital_service", kind="product"),
    TaxLineInput(key="line:1", net_cents=1_999, tax_class_code="standard", kind="product"),
)


def _taxed(rate_cents_line0: int, rate_cents_line1: int):
    return {"line:0": rate_cents_line0, "line:1": rate_cents_line1}


NONE = {"line:0": 0, "line:1": 0}
# 1999 at each rate, rounded half up.
GB20 = _taxed(2000, 400)  # 399.80
DE19 = _taxed(1900, 380)  # 379.81
FR20 = _taxed(2000, 400)
IE23 = _taxed(2300, 460)  # 459.77
FI255 = _taxed(2550, 510)  # 509.745
GR24 = _taxed(2400, 480)  # 479.76


@dataclass(frozen=True)
class Case:
    id: str
    seller: str
    registrations: tuple[str, ...]  # "GB", "DE", "IE", "OSS_IE", "OSS_DE"
    country: str
    business: bool
    tax_id_status: str  # "valid", "stale", "invalid", "unchecked", "unavailable"
    tax: dict
    rate: Optional[str] = None
    reverse_charge: bool = False
    notes: frozenset = field(default_factory=frozenset)
    registration: Optional[str] = None
    not_registered: bool = False
    region: Optional[str] = None
    tax_id: Optional[str] = None  # the buyer's number when it is not "<country>123456789"
    line1_class: str = "standard"
    reasons: frozenset = field(default_factory=frozenset)  # evidence reasons that must be recorded
    evidence_notes: frozenset = field(default_factory=frozenset)  # evidence notes besides not_registered
    declared: Optional[str] = None  # buyer_country_declared when the engine maps the country


C = [
    # Seller established in the UK.
    Case("uk-consumer", "GB", ("GB",), "GB", False, "unchecked", GB20, "20.0000", registration="GB"),
    Case("uk-consumer-ni", "GB", ("GB",), "GB", False, "unchecked", GB20, "20.0000", registration="GB", region="NIR"),
    Case("uk-business-valid", "GB", ("GB",), "GB", True, "valid", GB20, "20.0000", registration="GB"),
    Case(
        "uk-to-eu-business-valid", "GB", ("GB", "OSS_IE"), "DE", True, "valid", NONE, reverse_charge=True,
        notes=frozenset({"gb_outside_scope_b2b"}), registration="GB",
    ),
    Case("uk-to-eu-business-unchecked-no-oss", "GB", ("GB",), "DE", True, "unchecked", NONE, not_registered=True, registration="GB"),
    Case(
        "uk-to-eu-business-invalid-oss", "GB", ("GB", "OSS_IE"), "DE", True, "invalid", DE19, "19.0000",
        registration="OSS_IE",
    ),
    Case(
        "uk-to-eu-business-stale-oss", "GB", ("GB", "OSS_IE"), "DE", True, "stale", DE19, "19.0000",
        registration="OSS_IE",
    ),
    Case(
        "uk-to-eu-consumer-oss", "GB", ("GB", "OSS_IE"), "FR", False, "unchecked", FR20, "20.0000",
        registration="OSS_IE",
    ),
    Case(
        "uk-to-eu-consumer-domestic-beats-oss", "GB", ("GB", "IE", "OSS_IE"), "IE", False, "unchecked", IE23,
        "23.0000", registration="IE",
    ),
    Case("uk-to-eu-consumer-no-oss", "GB", ("GB",), "FR", False, "unchecked", NONE, not_registered=True, registration="GB"),
    Case("uk-to-row-consumer", "GB", ("GB",), "US", False, "unchecked", NONE, notes=frozenset({"outside_scope"}), registration="GB"),
    Case("uk-to-row-business", "GB", ("GB",), "US", True, "valid", NONE, notes=frozenset({"outside_scope"}), registration="GB"),
    # Seller established in an EU member state.
    Case("de-consumer", "DE", ("DE",), "DE", False, "unchecked", DE19, "19.0000", registration="DE"),
    Case("de-business-valid", "DE", ("DE", "OSS_DE"), "DE", True, "valid", DE19, "19.0000", registration="DE"),
    Case(
        "de-to-fr-business-valid", "DE", ("DE", "OSS_DE"), "FR", True, "valid", NONE, reverse_charge=True,
        notes=frozenset({"eu_reverse_charge"}), registration="DE",
    ),
    Case(
        "de-to-fr-business-unavailable-oss", "DE", ("DE", "OSS_DE"), "FR", True, "unavailable", FR20, "20.0000",
        registration="OSS_DE",
    ),
    Case(
        "de-to-fr-consumer-oss", "DE", ("DE", "OSS_DE"), "FR", False, "unchecked", FR20, "20.0000",
        registration="OSS_DE",
    ),
    Case(
        "de-to-fi-consumer-oss", "DE", ("DE", "OSS_DE"), "FI", False, "unchecked", FI255, "25.5000",
        registration="OSS_DE",
    ),
    Case(
        "de-to-gr-consumer-oss", "DE", ("DE", "OSS_DE"), "GR", False, "unchecked", GR24, "24.0000",
        registration="OSS_DE",
    ),
    Case(
        "de-to-fr-consumer-under-threshold", "DE", ("DE",), "FR", False, "unchecked", DE19, "19.0000",
        registration="DE",
    ),
    Case("de-to-uk-business-valid", "DE", ("DE",), "GB", True, "valid", NONE, notes=frozenset({"outside_scope"}), registration="DE"),
    Case("de-to-uk-consumer-gb-reg", "DE", ("DE", "GB"), "GB", False, "unchecked", GB20, "20.0000", registration="GB"),
    Case("de-to-uk-consumer-no-gb-reg", "DE", ("DE",), "GB", False, "unchecked", NONE, not_registered=True, registration="DE"),
    Case("de-to-uk-business-unchecked", "DE", ("DE",), "GB", True, "unchecked", NONE, not_registered=True, registration="DE"),
    Case("de-to-row-consumer", "DE", ("DE", "OSS_DE"), "US", False, "unchecked", NONE, notes=frozenset({"outside_scope"}), registration="DE"),
]

# Cases added after the legal review of billing phase 2 (Isle of Man, Monaco, tax ID country,
# missing buyer country, a class with no rule). Run with the appendix C cases.
IT22 = _taxed(2200, 440)  # 439.78

REVIEW = [
    # The Isle of Man is in the UK VAT area; Monaco is France for EU VAT.
    Case("uk-to-im-consumer", "GB", ("GB",), "IM", False, "unchecked", GB20, "20.0000", registration="GB",
         declared="IM"),
    Case("uk-to-im-business-gb-number", "GB", ("GB",), "IM", True, "valid", GB20, "20.0000", registration="GB",
         tax_id="GB123456789", declared="IM"),
    Case("de-to-mc-consumer-oss", "DE", ("DE", "OSS_DE"), "MC", False, "unchecked", FR20, "20.0000",
         registration="OSS_DE", declared="MC"),
    # A business abroad only when its VAT number's country is the buyer's country; else a consumer there.
    Case("de-to-fr-business-de-number", "DE", ("DE", "OSS_DE"), "FR", True, "valid", FR20, "20.0000",
         registration="OSS_DE", tax_id="DE123456789", reasons=frozenset({"tax_id_country_mismatch"})),
    Case("uk-to-de-business-de-number", "GB", ("GB", "OSS_IE"), "DE", True, "valid", NONE, reverse_charge=True,
         notes=frozenset({"gb_outside_scope_b2b"}), registration="GB", tax_id="DE123456789"),
    Case("de-to-it-business-gb-number", "DE", ("DE", "OSS_DE"), "IT", True, "valid", IT22, "22.0000",
         registration="OSS_DE", tax_id="GB123456789", reasons=frozenset({"tax_id_country_mismatch"})),
    Case("de-to-uk-business-de-number", "DE", ("DE",), "GB", True, "valid", NONE, not_registered=True,
         registration="DE", tax_id="DE123456789", reasons=frozenset({"tax_id_country_mismatch"})),
    # A buyer with no country is taxed as a consumer in the seller's country, and the evidence says so.
    Case("uk-buyer-country-missing", "GB", ("GB",), None, False, "unchecked", GB20, "20.0000", registration="GB",
         evidence_notes=frozenset({"buyer_country_missing"})),
    Case("de-buyer-country-missing", "DE", ("DE", "OSS_DE"), None, False, "unchecked", DE19, "19.0000",
         registration="DE", evidence_notes=frozenset({"buyer_country_missing"})),
    Case("de-business-country-missing-valid-number", "DE", ("DE", "OSS_DE"), None, True, "valid", DE19, "19.0000",
         registration="DE", tax_id="FR123456789", evidence_notes=frozenset({"buyer_country_missing"})),
    # A class with no rule in the chosen jurisdiction is still untaxed, but the evidence names it.
    Case("de-consumer-reduced-class", "DE", ("DE",), "DE", False, "unchecked", {"line:0": 1900, "line:1": 0},
         registration="DE", line1_class="reduced", evidence_notes=frozenset({"no_rule_for_class"})),
    Case("uk-to-fr-consumer-oss-zero-class", "GB", ("GB", "OSS_IE"), "FR", False, "unchecked",
         {"line:0": 2000, "line:1": 0}, registration="OSS_IE", line1_class="zero",
         evidence_notes=frozenset({"no_rule_for_class"})),
    Case("uk-consumer-exempt-class", "GB", ("GB",), "GB", False, "unchecked", {"line:0": 2000, "line:1": 0},
         registration="GB", line1_class="exempt"),
]

_REGISTRATIONS = {
    "GB": ("GB", "domestic", "GB123456789", "GB"),
    "DE": ("DE", "domestic", "DE123456789", "DE"),
    "IE": ("IE", "domestic", "IE1234567WA", "IE"),
    "OSS_IE": ("EU_OSS", "oss_non_union", "EU372000041", "IE"),
    "OSS_DE": ("EU_OSS", "oss_union", "DE123456789", "DE"),
}


@pytest.fixture(autouse=True)
def _no_plugins(monkeypatch):
    monkeypatch.setattr(plugin_providers, "providers_of", lambda kind: [])
    registry_module._builtin.cache_clear()


def _setup(db, case: Case) -> tuple[TaxContext, dict[str, int]]:
    seed_tax_basics(db)
    apply_pack(db, "uk_vat")
    apply_pack(db, "eu_vat")
    set_setting(db, "billing_tax_enabled", True)
    set_setting(db, "billing_tax_seller_country", case.seller)
    regs = {}
    for name in case.registrations:
        jurisdiction, scheme, number, country = _REGISTRATIONS[name]
        row = SellerTaxRegistration(
            jurisdiction=jurisdiction, scheme=scheme, tax_number=number, country=country,
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        db.add(row)
        db.flush()
        regs[name] = row.id

    check_id = None
    status = case.tax_id_status
    if status in ("valid", "stale", "invalid", "unavailable"):
        checked = STALE if status == "stale" else FRESH
        stored = "valid" if status == "stale" else status
        check = TaxIdCheck(
            country=(case.tax_id or case.country)[:2], normalised_id="123456789", id_type="eu_vat", validator="vies:vies",
            status=stored, checked_at=checked, expires_at=checked + timedelta(days=30),
        )
        db.add(check)
        db.flush()
        check_id = check.id
        status = stored
    buyer = BuyerTaxFacts(
        country=case.country,
        region=case.region,
        postcode=None,
        is_business=case.business,
        tax_id=(case.tax_id or f"{case.country}123456789") if case.business else None,
        tax_id_type="eu_vat" if case.business and (case.tax_id or case.country or "")[:2] not in ("GB", "US", "IM") else None,
        tax_id_status=status,
        tax_id_check_id=check_id,
        exempt_all=False,
        exemption_ids=(),
    )
    context = TaxContext(
        tax_point_at=TAX_POINT,
        currency="GBP" if case.seller == "GB" else "EUR",
        prices_include_tax=False,
        buyer=buyer,
        lines=LINES if case.line1_class == "standard" else (
            LINES[0], TaxLineInput(key="line:1", net_cents=1_999, tax_class_code=case.line1_class, kind="product"),
        ),
        purpose="order_charge",
        owner="retail",
        evidence={},
    )
    return context, regs


@pytest.mark.parametrize("case", C + REVIEW, ids=[c.id for c in C + REVIEW])
def test_appendix_c(db_session, case):
    context, regs = _setup(db_session, case)
    result = TaxEngine.calculate(db_session, context)

    for key, expected in case.tax.items():
        line = result.lines[key]
        assert line.tax_cents == expected, key
        assert sum(part.tax_cents for part in line.parts) == expected, key
        if case.rate is not None:
            assert [str(part.rate_percent) for part in line.parts] == [case.rate], key
        if case.reverse_charge:
            assert line.parts and all(part.reverse_charge and part.tax_cents == 0 for part in line.parts), key
        elif case.rate is not None:
            assert not any(part.reverse_charge for part in line.parts), key
    assert set(result.legal_note_keys) == set(case.notes)
    assert result.registration_id == (regs[case.registration] if case.registration else None)
    values = dict(result.evidence.values)
    evidence = json.dumps(values, default=str)
    assert ("not_registered" in evidence) == case.not_registered
    assert set(values.get("notes") or []) - {"not_registered"} == set(case.evidence_notes)
    reasons = set(values.get("reasons") or [])
    assert set(case.reasons) <= reasons
    assert ("tax_id_country_mismatch" in reasons) == ("tax_id_country_mismatch" in case.reasons)
    assert values.get("buyer_country_declared") == case.declared


def test_a_tax_id_without_a_check_row_is_read_from_its_prefix(db_session):
    """With no check row the number's own prefix gives its country: a GB number billed in Italy is a mismatch."""
    case = Case("de-to-it-gb-number-no-check", "DE", ("DE", "OSS_DE"), "IT", False, "unchecked", IT22)
    context, _regs = _setup(db_session, case)
    facts = dataclasses.replace(
        context.buyer, is_business=True, tax_id="GB123456789", tax_id_type="gb_vat", tax_id_status="valid",
        tax_id_checked_at=FRESH,
    )
    result = TaxEngine.calculate(db_session, dataclasses.replace(context, buyer=facts))
    assert [result.lines[key].tax_cents for key in ("line:0", "line:1")] == [2200, 440]
    values = result.evidence.values
    assert "tax_id_country_mismatch" in values["reasons"] and values["tax_id_country"] == "GB"


def test_no_rule_for_class_names_the_class_and_jurisdiction(db_session):
    case = next(c for c in REVIEW if c.id == "uk-to-fr-consumer-oss-zero-class")
    context, _regs = _setup(db_session, case)
    values = TaxEngine.calculate(db_session, context).evidence.values
    assert values["no_rule_for_class"] == [{"tax_class": "zero", "jurisdiction": "EU_OSS", "country": "FR"}]


def test_the_table_covers_every_row_of_appendix_c():
    ids = {c.id for c in C}
    for row in (
        "uk-consumer", "uk-business-valid", "uk-to-eu-business-valid", "uk-to-eu-business-unchecked-no-oss",
        "uk-to-eu-consumer-oss", "uk-to-eu-consumer-no-oss", "uk-to-row-consumer", "uk-to-row-business",
        "de-consumer", "de-business-valid", "de-to-fr-business-valid", "de-to-fr-consumer-oss",
        "de-to-fr-consumer-under-threshold", "de-to-uk-business-valid", "de-to-uk-consumer-gb-reg",
        "de-to-uk-consumer-no-gb-reg", "de-to-uk-business-unchecked", "de-to-row-consumer",
    ):
        assert row in ids
