"""Checkout tax: legacy lookup, shadow like for like, v2 quote equals invoice (design 3, 4.3, 4.5, 4.7)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.dao.commerce_dao import BillingProfileDAO
from app.models.commerce_coupon_tax import Coupon, TaxRate
from app.models.reseller import Invoice
from app.models.tax import InvoiceLineTax, TaxRule
from app.services.checkout_service import CheckoutService
from app.services.settings_store import set_setting
from app.services.tax.mirror import TaxRuleMirror
from tests.services.tax_helpers import (
    add_registration,
    add_rule,
    capture_shadow,
    enable_tax,
    invoice_v2,
    seed_classes,
    set_engine,
    stub_legal_notes,
)
from tests.services.test_billing_a0_money_path import _account, _product


@pytest.fixture(autouse=True)
def _retail(monkeypatch, db_session):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    stub_legal_notes(monkeypatch)
    seed_classes(db_session)


@pytest.fixture
def shadow(monkeypatch):
    return capture_shadow(monkeypatch)


def _rate(db, country, region, bps, *, mirror=True):
    rate = TaxRate(name=f"{country}-{region}-{bps}", country=country, region=region, rate_bps=bps, enabled=True)
    db.add(rate)
    db.flush()
    if mirror:
        TaxRuleMirror.sync(db, rate)
    return rate


def _buyer(db, country="GB", region=None, *, exempt=False):
    user, account = _account(db, "p2t")
    BillingProfileDAO.upsert(db, account.id, country=country, region=region)
    account.tax_exempt = exempt
    db.flush()
    return user, account


def _quote(db, account, product, plan, **kwargs):
    return CheckoutService.quote(db, account, product.id, plan.id, "monthly", None, **kwargs)


def _place(db, account, user, product, plan, **kwargs):
    return CheckoutService.place_order(
        db, account=account, user_id=user.id, frontend_product_id=product.id, price_plan_id=plan.id,
        cycle_interval="monthly", checkout_nonce=uuid.uuid4().hex, terms_version="1", **kwargs,
    )


# --- today's lookup no longer raises (ledger to-do 7cc01854) ---


def test_profile_without_region_takes_the_national_row(db_session):
    _rate(db_session, "US", None, 500, mirror=False)
    _rate(db_session, "US", "CA", 725, mirror=False)
    _user, account = _buyer(db_session, "US")
    assert CheckoutService._tax_cents(db_session, account, 10_000) == 500


def test_duplicate_national_rows_take_the_lowest_id(db_session):
    first = _rate(db_session, "DE", None, 1900, mirror=False)
    _rate(db_session, "DE", None, 700, mirror=False)
    _user, account = _buyer(db_session, "DE")
    assert CheckoutService._tax_rate(db_session, account).id == first.id
    assert CheckoutService._tax_cents(db_session, account, 10_000) == 1900


def test_profile_region_takes_its_regional_row_over_the_national(db_session):
    _rate(db_session, "US", None, 500, mirror=False)
    _rate(db_session, "US", "CA", 725, mirror=False)
    _user, account = _buyer(db_session, "US", "CA")
    assert CheckoutService._tax_cents(db_session, account, 10_000) == 725


def test_empty_region_row_is_a_literal_region(db_session):
    _rate(db_session, "FR", "", 2000, mirror=False)
    _user, with_region = _buyer(db_session, "FR", "Paris")
    _user, without = _buyer(db_session, "FR")
    assert CheckoutService._tax_cents(db_session, with_region, 10_000) == 0
    assert CheckoutService._tax_cents(db_session, without, 10_000) == 2000


# --- shadow: engine on mirrored rules, like for like (design 4.3 step 1) ---


LIKE_FOR_LIKE = [
    ("national", [("GB", None, 2000)], ("GB", None), {}, False),
    ("region row", [("US", None, 500), ("US", "CA", 725)], ("US", "CA"), {}, False),
    ("region row, no profile region", [("US", "CA", 725)], ("US", None), {}, False),
    ("national over region, no profile region", [("US", None, 500), ("US", "CA", 725)], ("US", None), {}, False),
    ("duplicate national rows", [("DE", None, 1900), ("DE", None, 700)], ("DE", None), {}, False),
    ("literal empty region", [("FR", "", 2000)], ("FR", "Paris"), {}, False),
    ("literal empty region, no profile region", [("FR", "", 2000)], ("FR", None), {}, False),
    ("exempt account", [("GB", None, 2000)], ("GB", None), {}, True),
    ("no country", [("GB", None, 2000)], (None, None), {}, False),
    ("odd rate", [("GB", None, 1750)], ("GB", None), {}, False),
    ("coupon by amount", [("GB", None, 1750)], ("GB", None), {"amount_off_cents": 333}, False),
    ("coupon by percent", [("GB", None, 1750)], ("GB", None), {"percent_off": 33}, False),
    ("zero subtotal", [("GB", None, 2000)], ("GB", None), {"percent_off": 100}, False),
]


@pytest.mark.parametrize("label,rates,profile,coupon,exempt", LIKE_FOR_LIKE, ids=[c[0] for c in LIKE_FOR_LIKE])
def test_shadow_engine_matches_tax_cents_on_mirrored_rules(db_session, shadow, label, rates, profile, coupon, exempt):
    for country, region, bps in rates:
        _rate(db_session, country, region, bps)
    user, account = _buyer(db_session, *profile, exempt=exempt)
    product, plan = _product(db_session, price_cents=1001, setup_cents=99)
    code = None
    if coupon:
        code = f"P2T{uuid.uuid4().hex[:6]}"
        db_session.add(Coupon(code=code, currency="USD", enabled=True, **coupon))
        db_session.flush()
    quote = _quote(db_session, account, product, plan, coupon_code=code)
    assert quote.tax_result is not None
    assert quote.tax_result.tax_cents == quote.tax_cents
    if quote.total_cents > 0:
        order = _place(db_session, account, user, product, plan, coupon_code=code)
        assert order.invoice_id is not None
    assert not [r for r in shadow if r["engine"] == "tax"]


@pytest.mark.parametrize(
    "rows,profile",
    [
        ([("DE", None, 1900), ("DE", None, 700)], ("DE", None)),
        # tax_rates is unique on (country, region), so only national rows (NULL region) repeat.
        ([("US", None, 500), ("US", None, 600), ("US", "CA", 725)], ("US", "TX")),
    ],
    ids=["duplicate national rows", "profile region over duplicate national rows"],
)
def test_shadow_ties_on_the_tax_rates_row_after_a_supersede(db_session, shadow, rows, profile):
    first, *_rest = [_rate(db_session, country, region, bps) for country, region, bps in rows]
    old_rule = TaxRuleMirror.current(db_session, first.id)
    first.name = "renamed"
    new_rule = TaxRuleMirror.sync(db_session, first)
    assert new_rule.supersedes_id == old_rule.id
    user, account = _buyer(db_session, *profile)
    product, plan = _product(db_session, price_cents=10_000)
    quote = _quote(db_session, account, product, plan)
    assert CheckoutService._tax_rate(db_session, account).id == first.id
    expected = 10_000 * first.rate_bps // 10_000
    assert quote.tax_cents == expected
    assert quote.tax_result.tax_cents == expected
    (part,) = quote.tax_result.line("line:0").parts
    assert part.rule_id == new_rule.id
    _place(db_session, account, user, product, plan)
    assert not [r for r in shadow if r["engine"] == "tax"]


def test_shadow_records_t1_checkout_when_the_engine_differs(db_session, shadow, monkeypatch):
    _rate(db_session, "GB", None, 2000)
    user, account = _buyer(db_session, "GB")
    product, plan = _product(db_session, price_cents=1000)
    monkeypatch.setattr(CheckoutService, "_tax_cents", staticmethod(lambda *_a, **_k: 123))
    _place(db_session, account, user, product, plan)
    (record,) = [r for r in shadow if r["engine"] == "tax"]
    assert record["check"] == "t1_checkout"
    assert record["legacy"] == {"tax_cents": 123} and record["v2"]["tax_cents"] == 200


def test_shadow_records_a_rule_divergence_the_engine_explains(db_session, shadow):
    _rate(db_session, "GB", None, 2000)
    enable_tax(db_session, "")
    add_rule(db_session, name="admin 25%", rate="25", source="admin", priority=50)
    user, account = _buyer(db_session, "GB")
    product, plan = _product(db_session, price_cents=1000)
    _place(db_session, account, user, product, plan)
    (record,) = [r for r in shadow if r["engine"] == "tax"]
    assert record["check"] == "t1_rule_divergence"
    assert record["v2"]["explained_by"] == [f"rule:{db_session.scalar(select(TaxRule.id).where(TaxRule.name == 'admin 25%'))}"]


def _valid_business_profile(db, account):
    from datetime import datetime, timezone

    BillingProfileDAO.upsert(
        db, account.id, is_business=True, tax_id="DE123456789", tax_id_status="valid",
        tax_id_checked_at=datetime.now(timezone.utc),
    )


def test_shadow_counts_a_reverse_charge_without_a_rule_as_explained(db_session, shadow):
    _rate(db_session, "DE", None, 1900)
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB")
    user, account = _buyer(db_session, "DE")
    _valid_business_profile(db_session, account)
    product, plan = _product(db_session, price_cents=1000)
    _place(db_session, account, user, product, plan)
    (record,) = [r for r in shadow if r["engine"] == "tax"]
    assert record["check"] == "t1_rule_divergence"
    assert record["legacy"] == {"tax_cents": 190} and record["v2"]["tax_cents"] == 0
    assert record["v2"]["explained_by"] == ["reverse_charge"]


def test_shadow_records_legacy_raising_as_its_own_outcome(db_session, shadow, monkeypatch):
    _rate(db_session, "GB", None, 2000)
    _user, account = _buyer(db_session, "GB")
    product, plan = _product(db_session)

    def raising(*_a, **_k):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(CheckoutService, "_tax_cents", staticmethod(raising))
    with pytest.raises(RuntimeError):
        _quote(db_session, account, product, plan)
    (record,) = [r for r in shadow if r["engine"] == "tax"]
    assert record["check"] == "t1_checkout" and record["context"] == "legacy_raised"
    assert record["legacy"] == {"raised": "RuntimeError"} and record["v2"]["tax_cents"] == 200


def test_shadow_never_fails_a_checkout_when_the_engine_raises(db_session, shadow, monkeypatch):
    from app.services.tax.engine import TaxEngine

    _rate(db_session, "GB", None, 2000)
    _user, account = _buyer(db_session, "GB")
    product, plan = _product(db_session)
    monkeypatch.setattr(TaxEngine, "calculate", staticmethod(lambda *_a: 1 / 0))
    quote = _quote(db_session, account, product, plan)
    assert quote.tax_cents == 200 and quote.tax_result is None


def test_legacy_mode_records_no_evidence_and_runs_no_engine(db_session, shadow, monkeypatch):
    from app.services.tax.engine import TaxEngine

    set_engine(db_session, "tax", "legacy")
    _rate(db_session, "GB", None, 2000)
    user, account = _buyer(db_session, "GB")
    product, plan = _product(db_session)
    monkeypatch.setattr(TaxEngine, "calculate", staticmethod(lambda *_a: 1 / 0))
    order = _place(db_session, account, user, product, plan, client_ip="203.0.113.9")
    assert order.tax_evidence is None
    assert db_session.get(Invoice, order.invoice_id).amount_cents == 1200


def test_shadow_captures_order_evidence(db_session, shadow):
    _rate(db_session, "GB", None, 2000)
    user, account = _buyer(db_session, "GB")
    product, plan = _product(db_session)
    order = _place(db_session, account, user, product, plan, client_ip="203.0.113.9", ip_country="fr")
    assert order.tax_evidence == {"billing_country": "GB", "ip_address": "203.0.113.9", "ip_country": "FR"}


# --- v2: quote equals invoice ---


@pytest.fixture
def tax_v2(db_session):
    invoice_v2(db_session)
    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB")
    add_rule(db_session, name="UK VAT 20%", jurisdiction="GB", source="pack", rounding_point="line")


@pytest.mark.parametrize("setup,price", [(0, 1000), (99, 1001), (333, 777)])
def test_v2_quote_equals_invoice(db_session, tax_v2, setup, price):
    user, account = _buyer(db_session, "GB")
    product, plan = _product(db_session, price_cents=price, setup_cents=setup)
    plan.currency = "GBP"
    account.currency = "GBP"
    db_session.flush()
    quote = _quote(db_session, account, product, plan)
    order = _place(db_session, account, user, product, plan)
    invoice = db_session.get(Invoice, order.invoice_id)
    assert (invoice.tax_cents, invoice.amount_cents) == (quote.tax_cents, quote.total_cents)
    assert invoice.tax_summary == quote.tax_summary
    rows = db_session.execute(
        select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice.id).order_by(InvoiceLineTax.id)
    ).scalars().all()
    assert [(r.base_cents, r.tax_cents) for r in rows] == [(t["base_cents"], t["tax_cents"]) for t in quote.tax_lines]


def test_v2_quote_carries_reverse_charge_and_notes(db_session, tax_v2):
    from datetime import datetime, timezone

    add_rule(
        db_session, name="Reverse charge", rate="0", country=None, jurisdiction="GB", source="pack",
        customer_type="business", reverse_charge=True, legal_note_key="gb_outside_scope_b2b",
    )
    _user, account = _buyer(db_session, "DE")
    BillingProfileDAO.upsert(
        db_session, account.id, is_business=True, tax_id="DE123456789", tax_id_status="valid",
        tax_id_checked_at=datetime.now(timezone.utc),
    )
    product, plan = _product(db_session)
    quote = _quote(db_session, account, product, plan)
    assert quote.reverse_charge is True and quote.tax_cents == 0
    assert quote.legal_notes == ["gb_outside_scope_b2b"]


def test_v2_reverse_charge_without_a_rule_sets_the_quote_flag_and_the_rows(db_session, tax_v2):
    user, account = _buyer(db_session, "DE")
    _valid_business_profile(db_session, account)
    product, plan = _product(db_session)
    quote = _quote(db_session, account, product, plan)
    assert quote.reverse_charge is True and quote.tax_cents == 0
    assert quote.legal_notes == ["gb_outside_scope_b2b"]
    assert {item["name"] for item in quote.tax_lines} == {"Outside the scope of UK VAT"}
    order = _place(db_session, account, user, product, plan)
    rows = db_session.execute(
        select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == order.invoice_id)
    ).scalars().all()
    assert rows and all(r.reverse_charge and r.tax_cents == 0 and r.tax_rule_id is None for r in rows)


def test_v2_needs_invoice_engine_v2(db_session):
    set_engine(db_session, "tax", "v2")
    assert CheckoutService._tax_mode(db_session) == "shadow"


# --- the tax_rates mirror (design 3) ---


def test_mirror_change_to_a_used_rule_supersedes_it(db_session):
    rate = _rate(db_session, "GB", None, 2000)
    old = TaxRuleMirror.current(db_session, rate.id)
    rate.rate_bps = 1750
    new = TaxRuleMirror.sync(db_session, rate)
    db_session.flush()
    assert new.id != old.id and new.supersedes_id == old.id
    db_session.refresh(old)
    assert old.legacy_tax_rate_id is None and old.valid_to is not None
    assert new.legacy_tax_rate_id == rate.id and str(new.rate_percent).startswith("17.5")
    assert TaxRuleMirror.sync(db_session, rate).id == new.id


def test_mirror_keeps_an_empty_region_literal(db_session):
    rate = _rate(db_session, "FR", "", 2000)
    rule = TaxRuleMirror.current(db_session, rate.id)
    assert rule.region == "" and rule.priority == 0


@pytest.mark.parametrize("before,after", [(None, ""), ("", None)])
def test_mirror_sees_a_change_between_no_region_and_an_empty_region(db_session, before, after):
    rate = _rate(db_session, "FR", before, 2000)
    old = TaxRuleMirror.current(db_session, rate.id)
    rate.region = after
    new = TaxRuleMirror.sync(db_session, rate)
    assert new.id != old.id and new.supersedes_id == old.id
    assert new.region == after
    # Legacy and engine still agree for a buyer with and without a region (a literal '' matches no region only).
    for profile in (("FR", None), ("FR", "Paris")):
        _user, account = _buyer(db_session, *profile)
        product, plan = _product(db_session, price_cents=10_000)
        quote = _quote(db_session, account, product, plan)
        assert quote.tax_result.tax_cents == quote.tax_cents


def test_prices_include_tax_setting_is_read(db_session):
    set_setting(db_session, "billing_tax_prices_include_tax", True)
    assert CheckoutService._prices_include_tax(db_session) is True
