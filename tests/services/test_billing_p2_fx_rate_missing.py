"""The detector billing_tax_fx_rate_missing and the tax switch item exchange_rate_stored (design 4.10)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import settings
from app.services import billing_shadow
from app.services.alerting import billing_integrity as bi
from app.services.alerting.catalogue import CATALOGUE
from app.services.tax import exchange_rates
from tests.services.tax_helpers import add_registration, seed_classes, set_engine

NOV_10 = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
NOV_25 = datetime(2026, 11, 25, 9, tzinfo=timezone.utc)


def _fire(db, now=NOV_10):
    return [f.resource_id for f in bi.tax_fx_rate_missing(db, {}, now)]


def _store(db, period):
    exchange_rates.store_month(db, period, [("USD", "1.3396")], fetched_at=NOV_10)


@pytest.fixture
def uk(db_session):
    seed_classes(db_session)
    add_registration(db_session, "GB")
    return db_session


def test_enabled_by_default_and_registered():
    assert CATALOGUE["billing_tax_fx_rate_missing"].enabled_by_default is True
    assert bi.BILLING_DETECTORS["billing_tax_fx_rate_missing"] is bi.tax_fx_rate_missing


@pytest.mark.parametrize("mode", ["shadow", "v2"])
def test_fires_in_shadow_and_v2_when_this_months_rate_is_missing(uk, mode):
    set_engine(uk, "tax", mode)
    assert _fire(uk) == ["tax_exchange_rate:2026-11:USD"]
    _store(uk, "2026-11")
    assert _fire(uk) == []


def test_from_the_25th_it_also_wants_next_month(uk):
    set_engine(uk, "tax", "v2")
    _store(uk, "2026-11")
    assert _fire(uk, datetime(2026, 11, 24, 23, 59, tzinfo=timezone.utc)) == []
    assert _fire(uk, NOV_25) == ["tax_exchange_rate:2026-12:USD"]
    _store(uk, "2026-12")
    assert _fire(uk, NOV_25) == []


def test_silent_with_tax_legacy(uk):
    set_engine(uk, "tax", "legacy")
    assert _fire(uk) == [] and _fire(uk, NOV_25) == []


def test_silent_without_a_gb_registration(db_session):
    seed_classes(db_session)
    set_engine(db_session, "tax", "v2")
    add_registration(db_session, "DE", number="DE123456789")
    assert _fire(db_session) == []


def test_silent_with_a_disabled_or_expired_gb_registration(db_session):
    seed_classes(db_session)
    set_engine(db_session, "tax", "v2")
    row = add_registration(db_session, "GB")
    row.enabled = False
    db_session.flush()
    assert _fire(db_session) == []


def test_silent_with_gbp_as_the_operator_currency(uk, monkeypatch):
    set_engine(uk, "tax", "v2")
    monkeypatch.setattr(settings, "commerce_default_currency", "GBP")
    assert _fire(uk) == [] and _fire(uk, NOV_25) == []


def _switch_item(db, now=NOV_10):
    items = {item["item"]: item for item in billing_shadow.evaluate_switch(db, "tax", now)}
    return items["exchange_rate_stored"]


def test_the_switch_blocks_on_a_missing_current_month(uk):
    set_engine(uk, "tax", "shadow")
    item = _switch_item(uk)
    assert not item["ok"] and item["missing_periods"] == ["2026-11"] and item["currency"] == "USD"
    _store(uk, "2026-11")
    assert _switch_item(uk)["ok"]
    # Next month's rate is the detector's early warning, not a switch item.
    assert _switch_item(uk, NOV_25)["ok"]


def test_the_switch_item_passes_without_uk_tax(db_session, monkeypatch):
    seed_classes(db_session)
    assert _switch_item(db_session)["ok"]
    add_registration(db_session, "GB")
    monkeypatch.setattr(settings, "commerce_default_currency", "GBP")
    assert _switch_item(db_session)["ok"]
