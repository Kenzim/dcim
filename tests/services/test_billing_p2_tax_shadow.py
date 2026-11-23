"""Tax shadow scan (t6_scan), switch criterion and detectors (design 5, 15.4)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.commerce_invoice_ext import InvoiceLine, InvoiceLineKind
from app.models.reseller import Invoice, InvoicePurpose
from app.models.tax import InvoiceLineTax
from app.services import billing_shadow
from app.services.alerting import billing_integrity as bi
from app.services.invoice_service import InvoiceService, NewLine
from tests.services.tax_helpers import (
    add_registration,
    add_rule,
    enable_tax,
    invoice_v2,
    retail_account,
    seed_classes,
    set_engine,
    stub_legal_notes,
)

PARAMS = {"max_rows": 50}


@pytest.fixture
def invoice(db_session, monkeypatch):
    stub_legal_notes(monkeypatch)
    seed_classes(db_session)
    invoice_v2(db_session)
    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB")
    add_rule(db_session, name="UK VAT 20%", jurisdiction="GB", source="pack", rounding_point="line")
    _user, account = retail_account(db_session, country="GB")
    return InvoiceService.create(
        db_session, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1000,
        currency="GBP", lines=[NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT)],
    )


def _now():
    return datetime.now(timezone.utc)


def _break_row(db, invoice):
    """Tamper with a frozen row below the guard (raw SQL), as a bug or a manual edit would."""
    row = db.execute(select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice.id)).scalar_one()
    db.execute(InvoiceLineTax.__table__.update().where(InvoiceLineTax.id == row.id).values(tax_cents=150))
    db.expire_all()


def test_consistent_invoice_has_no_findings(db_session, invoice):
    lines = db_session.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).all()
    from app.services.tax.audit import invoice_tax_findings

    assert list(invoice_tax_findings(db_session, invoice, lines, rerun=True)) == []
    for detector in (bi.tax_line_sum, bi.tax_summary_consistent, bi.tax_evidence_conflict, bi.tax_not_registered):
        assert detector(db_session, PARAMS, _now()) == []
    assert billing_shadow._tax_scan(db_session, _now()) == (1, [])


def test_a_rescan_of_an_xi_invoice_rebuilds_the_declared_country(db_session, monkeypatch):
    from app.services.tax.audit import invoice_tax_findings
    from app.services.tax.context import TaxContextBuilder
    from app.services.tax.engine import TaxEngine

    stub_legal_notes(monkeypatch)
    seed_classes(db_session)
    invoice_v2(db_session)
    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB")
    add_rule(db_session, name="UK VAT 20%", jurisdiction="GB", source="pack", rounding_point="line")
    _user, account = retail_account(db_session, country="XI")
    invoice = InvoiceService.create(
        db_session, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1000,
        currency="GBP", lines=[NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT)],
    )
    assert invoice.tax_cents == 200
    assert (invoice.tax_evidence["buyer_country"], invoice.tax_evidence["buyer_country_declared"]) == ("GB", "XI")
    lines = db_session.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).all()
    rebuilt = TaxContextBuilder.for_stored(db_session, invoice, lines)
    assert rebuilt.buyer.country == "XI"
    again = TaxEngine.calculate(db_session, rebuilt)
    assert again.context_hash == invoice.tax_evidence["context_hash"]
    assert again.evidence.values["buyer_country_declared"] == "XI"
    assert list(invoice_tax_findings(db_session, invoice, lines, rerun=True)) == []


def _context_changed(db, subject_id, *, hours_ago=1, check=None):
    from datetime import timedelta

    from app.models.billing_shadow import BillingShadowMismatch
    from app.services.tax.shadow import ENGINE, T5_CONTEXT_CHANGED

    seen = _now() - timedelta(hours=hours_ago)
    db.add(
        BillingShadowMismatch(
            engine=ENGINE, check_key=check or T5_CONTEXT_CHANGED, subject_type="invoice", subject_id=subject_id,
            legacy_value={}, v2_value={}, first_seen_at=seen, last_seen_at=seen,
        )
    )
    db.flush()


def test_context_changed_alert_fires_above_ten_invoices_a_day(db_session):
    from app.services.alerting import detectors
    from app.services.alerting.catalogue import CATALOGUE

    params = CATALOGUE["billing_tax_context_changed"].defaults()
    assert params == {"daily_threshold": 10}
    for subject_id in range(1, 11):
        _context_changed(db_session, subject_id)
    _context_changed(db_session, 100, hours_ago=25)
    _context_changed(db_session, 101, check="t1_checkout")
    assert bi.tax_context_changed(db_session, params, _now()) == []
    _context_changed(db_session, 11)
    (finding,) = detectors.DETECTORS["billing_tax_context_changed"](db_session, params, _now())
    assert finding.details == {"invoices": 11, "threshold": 10}


def test_broken_rows_are_found_by_scan_and_detectors(db_session, invoice):
    _break_row(db_session, invoice)
    checked, findings = billing_shadow._tax_scan(db_session, _now())
    contexts = {f["context"] for f in findings}
    assert checked == 1 and any(c.startswith("line_sum:") for c in contexts) and "summary" in contexts
    assert [f.resource_id for f in bi.tax_line_sum(db_session, PARAMS, _now())] == [str(invoice.id)]
    assert [f.resource_id for f in bi.tax_summary_consistent(db_session, PARAMS, _now())] == [str(invoice.id)]


def test_rerun_uses_the_frozen_evidence_not_todays_profile(db_session, invoice):
    from app.dao.commerce_dao import BillingProfileDAO

    BillingProfileDAO.upsert(db_session, invoice.billing_account_id, country="US")
    db_session.flush()
    assert billing_shadow._tax_scan(db_session, _now()) == (1, [])


def test_rerun_finds_a_changed_rate_on_the_same_rule(db_session, invoice):
    from app.models.tax import TaxRule

    rule = db_session.scalar(select(TaxRule).where(TaxRule.name == "UK VAT 20%"))
    db_session.execute(TaxRule.__table__.update().where(TaxRule.id == rule.id).values(rate_percent=25))
    db_session.expire_all()
    _checked, findings = billing_shadow._tax_scan(db_session, _now())
    assert [f["context"] for f in findings] == ["rerun"]


def test_evidence_and_not_registered_detectors(db_session, invoice):
    evidence = dict(invoice.tax_evidence)
    db_session.execute(
        Invoice.__table__.update().where(Invoice.id == invoice.id).values(
            tax_evidence={**evidence, "conflict": True, "ip_country": "FR", "notes": ["not_registered"]}
        )
    )
    db_session.expire_all()
    assert [f.resource_id for f in bi.tax_evidence_conflict(db_session, PARAMS, _now())] == [str(invoice.id)]
    assert [f.resource_id for f in bi.tax_not_registered(db_session, PARAMS, _now())] == [str(invoice.id)]


def test_buyer_country_missing_detector(db_session, invoice):
    assert bi.tax_buyer_country_missing(db_session, PARAMS, _now()) == []
    evidence = dict(invoice.tax_evidence)
    db_session.execute(
        Invoice.__table__.update().where(Invoice.id == invoice.id).values(
            tax_evidence={**evidence, "buyer_country": None, "notes": ["buyer_country_missing"]}
        )
    )
    db_session.expire_all()
    (finding,) = bi.tax_buyer_country_missing(db_session, PARAMS, _now())
    assert finding.resource_id == str(invoice.id) and finding.details["taxed_as"] == "GB"
    db_session.execute(Invoice.__table__.update().where(Invoice.id == invoice.id).values(tax_engine="legacy"))
    db_session.expire_all()
    assert bi.tax_buyer_country_missing(db_session, PARAMS, _now()) == []


def test_no_rule_for_class_detector(db_session, invoice):
    assert bi.tax_no_rule_for_class(db_session, PARAMS, _now()) == []
    evidence = dict(invoice.tax_evidence)
    missing = [{"tax_class": "reduced", "jurisdiction": "GB", "country": "GB"}]
    db_session.execute(
        Invoice.__table__.update().where(Invoice.id == invoice.id).values(
            tax_evidence={**evidence, "notes": ["no_rule_for_class"], "no_rule_for_class": missing}
        )
    )
    db_session.expire_all()
    (finding,) = bi.tax_no_rule_for_class(db_session, PARAMS, _now())
    assert finding.resource_id == str(invoice.id) and finding.details["missing"] == missing
    assert "reduced" in finding.message


def test_switch_lists_products_whose_class_has_no_rule_where_registered(db_session):
    from app.models.product_catalog import Product

    classes = seed_classes(db_session)
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB")
    add_registration(db_session, "EU_OSS", scheme="oss_non_union")
    add_rule(db_session, name="UK 20%", jurisdiction="GB", source="pack")
    add_rule(db_session, name="OSS FR", country="FR", jurisdiction="EU_OSS", source="pack")
    db_session.add_all([
        Product(name="Plain", code="plain"),
        Product(name="Book", code="book", tax_class_id=classes["reduced"]),
        Product(name="Old", code="old", tax_class_id=classes["zero"], enabled=False),
        Product(name="Fee", code="fee", tax_class_id=classes["exempt"]),
    ])
    db_session.flush()
    item = _items(db_session)["product_classes_have_rules"]
    assert not item["ok"]
    assert [(m["product_code"], m["tax_class"], m["jurisdiction"]) for m in item["missing"]] == [
        ("book", "reduced", "EU_OSS"), ("book", "reduced", "GB"),
    ]
    add_rule(db_session, name="UK 5%", rate="5", tax_class="reduced", jurisdiction="GB", source="pack")
    add_rule(db_session, name="OSS FR 5.5", rate="5.5", country="FR", tax_class="reduced", jurisdiction="EU_OSS",
             source="pack")
    assert _items(db_session)["product_classes_have_rules"]["ok"]


def test_switch_needs_the_operators_acknowledgement(db_session):
    from app.services.settings_store import set_setting

    seed_classes(db_session)
    item = _items(db_session)["adviser_signoff"]
    assert not item["ok"] and item["setting"] == "billing_tax_adviser_signoff"
    assert any(c.startswith("18 q8") for c in item["covers"]) and any(c.startswith("18 q10") for c in item["covers"])
    assert sum(c.startswith("18.1 q") for c in item["covers"]) == 9
    set_setting(db_session, "billing_tax_adviser_signoff", "   ")
    assert not _items(db_session)["adviser_signoff"]["ok"]
    set_setting(db_session, "billing_tax_adviser_signoff", "Smith & Co ref VAT-12, 2026-12-01")
    item = _items(db_session)["adviser_signoff"]
    assert item["ok"] and item["signoff"] == "Smith & Co ref VAT-12, 2026-12-01"


def test_signoff_item_is_the_operators_acknowledgement_and_still_blocks_the_switch(db_session):
    from app.services.settings_store import set_setting

    seed_classes(db_session)
    items = billing_shadow.evaluate_switch(db_session, "tax", _now())
    item = next(i for i in items if i["item"] == "adviser_signoff")
    assert item["label"] == "Operator has reviewed the tax defaults"
    assert "own tax adviser" in item["detail"] and "not legal advice" in item["detail"]
    for text in (item["label"], item["detail"], *item["covers"]):
        assert "sign-off" not in text.lower() and "signed off" not in text.lower()
    assert not item["ok"] and item["signoff"] is None
    assert "adviser_signoff" in [i["item"] for i in items if not i["ok"]]

    set_setting(db_session, "billing_tax_adviser_signoff", "  Reviewed by J Smith, 2026-12-01  ")
    items = billing_shadow.evaluate_switch(db_session, "tax", _now())
    item = next(i for i in items if i["item"] == "adviser_signoff")
    assert item["ok"] and item["signoff"] == "Reviewed by J Smith, 2026-12-01"
    assert "adviser_signoff" not in [i["item"] for i in items if not i["ok"]]


def _home(db, now=None):
    return [f.resource_id for f in bi.tax_home_registration_missing(db, {}, now or _now())]


@pytest.mark.parametrize("mode", ["shadow", "v2"])
def test_home_registration_missing_fires_in_shadow_and_v2(db_session, mode):
    from app.models.tax import SellerTaxRegistration

    seed_classes(db_session)
    set_engine(db_session, "tax", mode)
    enable_tax(db_session, "DE")
    assert _home(db_session) == ["tax_registration:DE"]
    add_registration(db_session, "EU_OSS", scheme="oss_union", country="DE")
    assert _home(db_session) == ["tax_registration:DE"]
    home = add_registration(db_session, "DE")
    assert _home(db_session) == []
    home.enabled = False
    db_session.flush()
    assert _home(db_session) == ["tax_registration:DE"]
    assert db_session.get(SellerTaxRegistration, home.id) is not None


def test_home_registration_missing_is_silent_with_tax_legacy_or_not_set_up(db_session):
    from app.services.alerting.catalogue import CATALOGUE
    from app.services.settings_store import set_setting

    assert CATALOGUE["billing_tax_home_registration_missing"].enabled_by_default is True
    seed_classes(db_session)
    set_engine(db_session, "tax", "shadow")
    assert _home(db_session) == []
    enable_tax(db_session, "GB")
    set_engine(db_session, "tax", "legacy")
    assert _home(db_session) == []
    set_engine(db_session, "tax", "v2")
    set_setting(db_session, "billing_tax_enabled", False)
    assert _home(db_session) == []


def test_a_tax_id_country_mismatch_raises_the_evidence_conflict_alert(db_session, invoice):
    evidence = dict(invoice.tax_evidence)
    db_session.execute(
        Invoice.__table__.update().where(Invoice.id == invoice.id).values(
            tax_cents=0,
            tax_evidence={**evidence, "reasons": ["tax_id_country_mismatch"], "tax_id_country": "DE",
                          "tax_id_country_mismatch": True},
        )
    )
    db_session.expire_all()
    (finding,) = bi.tax_evidence_conflict(db_session, PARAMS, _now())
    assert finding.resource_id == str(invoice.id)
    assert finding.details["tax_id_country"] == "DE" and "DE" in finding.message


def test_tax_is_iterated_and_its_non_blocking_checks_do_not_block(db_session):
    assert "tax" in billing_shadow.ENGINES
    assert {"t1_rule_divergence", "t2_uncovered", "t5_context_changed"} <= billing_shadow.EXPECTED_DIFFERENCE_KEYS
    assert "t1_checkout" not in billing_shadow.EXPECTED_DIFFERENCE_KEYS


def _items(db, **kwargs):
    return {item["item"]: item for item in billing_shadow.evaluate_switch(db, "tax", _now(), **kwargs)}


def test_switch_criterion_items(db_session):
    seed_classes(db_session)
    items = _items(db_session)
    assert items["mode_is_shadow"]["ok"]
    assert not items["invoice_engine_v2"]["ok"]
    assert not items["tax_enabled_or_mirrored_only_confirmed"]["ok"]
    assert _items(db_session, confirm_mirrored_only=True)["tax_enabled_or_mirrored_only_confirmed"]["ok"]
    assert items["registrations_for_rules"]["ok"] and items["changes_confirmed"]["ok"]

    enable_tax(db_session, "GB")
    add_rule(db_session, name="pack", jurisdiction="GB", source="pack")
    reg = _items(db_session)["registrations_for_rules"]
    assert not reg["ok"] and reg["unregistered"] == ["GB"]
    add_registration(db_session, "GB")
    assert _items(db_session)["registrations_for_rules"]["ok"]


def test_switch_needs_divergence_ids_and_uncovered_confirmed(db_session):
    from app.models.billing_shadow import BillingShadowMismatch

    seed_classes(db_session)
    now = _now()
    rows = [
        BillingShadowMismatch(engine="tax", check_key=key, subject_type="invoice", subject_id=i, legacy_value={},
                              v2_value={}, first_seen_at=now, last_seen_at=now)
        for i, key in enumerate(("t1_rule_divergence", "t2_uncovered"), start=1)
    ]
    db_session.add_all(rows)
    db_session.flush()
    item = _items(db_session)["changes_confirmed"]
    assert not item["ok"] and item["unconfirmed_divergence"] == [rows[0].id] and item["uncovered"] == 1
    assert not _items(db_session, confirm_divergence=[rows[0].id])["changes_confirmed"]["ok"]
    assert _items(db_session, confirm_divergence=[rows[0].id], confirm_uncovered=True)["changes_confirmed"]["ok"]
    # Non-blocking checks are not counted as open mismatches.
    assert _items(db_session)["no_open_mismatches"]["ok"]


def test_open_v2_taxed_invoices_counted_for_rollback(db_session, invoice):
    from app.models.reseller import InvoiceStatus

    assert invoice.status == InvoiceStatus.OPEN
    assert billing_shadow.open_v2_taxed_invoices(db_session) == 1


def test_tax_alerts_item_lists_the_disabled_alerts_and_passes_when_all_are_on(db_session):
    from tests.services.tax_helpers import TAX_ALERT_KEYS, enable_tax_alerts

    seed_classes(db_session)
    item = _items(db_session)["tax_alerts_enabled"]
    assert not item["ok"]
    assert item["disabled"] == [
        key for key in TAX_ALERT_KEYS if key not in ("billing_tax_fx_rate_missing", "billing_tax_home_registration_missing")
    ]
    assert item["required"] == list(TAX_ALERT_KEYS)
    enable_tax_alerts(db_session)
    item = _items(db_session)["tax_alerts_enabled"]
    assert item["ok"] and item["disabled"] == []


def test_the_tax_alert_list_is_every_billing_tax_detector():
    from app.services.alerting.billing_integrity import BILLING_DETECTORS
    from app.services.alerting.catalogue import CATALOGUE
    from tests.services.tax_helpers import TAX_ALERT_KEYS

    assert set(billing_shadow.INVOICE_TAX_ALERTS) == set(TAX_ALERT_KEYS)
    assert set(billing_shadow.INVOICE_TAX_ALERTS) == {k for k in CATALOGUE if k.startswith("billing_tax_")}
    assert set(billing_shadow.INVOICE_TAX_ALERTS) <= set(BILLING_DETECTORS)
