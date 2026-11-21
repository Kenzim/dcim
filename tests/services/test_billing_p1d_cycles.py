"""Billing phase 1, D1: cycle vocabulary, resolve_cycle by unit and count, long-cycle gate, admin API."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

os.environ["COMMERCE_RETAIL_ENABLED"] = "true"

from app.core.config import settings
from app.models.product_catalog import Product, ProductFamily
from app.models.storefront import (
    FrontendProduct,
    FrontendProductVisibility,
    PricePlan,
    PricePlanCycle,
    PricePlanInterval,
    PricePlanPricingModel,
)
from app.services.billing.cycles import CycleError, resolve_cycle, sellable_cycles
from app.services.billing.intervals import (
    INTERVAL_MONTHS,
    advance_months,
    interval_months,
    normalise_interval,
)
from app.services.commerce_recurring_service import CommerceRecurringService
from app.services.settings_store import set_setting

M = PricePlanPricingModel


def _plan(db: Session, model=M.RECURRING, setup=100) -> PricePlan:
    suffix = uuid.uuid4().hex[:8]
    family = ProductFamily(
        name=f"P1D {suffix}", code=f"p1d-{suffix}", service_type="vm", enabled=True, defaults={}, constraints={}
    )
    db.add(family)
    db.flush()
    catalog = Product(family_id=family.id, name=f"P1D {suffix}", code=f"p1dp-{suffix}", enabled=True, overrides={})
    db.add(catalog)
    db.flush()
    fp = FrontendProduct(
        name=f"P1D {suffix}",
        slug=f"p1d-{suffix}",
        short_description="d",
        description_md="m",
        description_html="<p>m</p>",
        product_id=catalog.id,
        service_type="vm",
        enabled=True,
        visibility=FrontendProductVisibility.PUBLIC,
        sort_order=0,
        features={},
        specs={},
        require_discord=False,
    )
    db.add(fp)
    db.flush()
    plan = PricePlan(
        frontend_product_id=fp.id, currency="USD", pricing_model=model, setup_cents=setup, enabled=True, name="P"
    )
    db.add(plan)
    db.flush()
    return plan


def _cycle(db, plan, label, price, **extra) -> PricePlanCycle:
    row = PricePlanCycle(price_plan_id=plan.id, interval=PricePlanInterval(label), price_cents=price, **extra)
    db.add(row)
    db.commit()
    db.refresh(plan)
    return row


def _long(db, on: bool) -> None:
    set_setting(db, "billing_long_cycles_enabled", on)
    db.commit()


# ------------------------------------------------------------------ intervals


def test_vocabulary_and_unknown_labels():
    assert INTERVAL_MONTHS["biennially"] == 24 and INTERVAL_MONTHS["triennially"] == 36
    assert [normalise_interval(v) for v in (None, "", "  ", "Monthly", "ANNUALLY")] == [
        "monthly", "monthly", "monthly", "monthly", "annually",
    ]
    assert interval_months("triennially") == 36
    with pytest.raises(ValueError):
        normalise_interval("fortnightly")
    with pytest.raises(ValueError):
        interval_months("weekly")


def test_a_triennial_service_advances_36_months_with_the_anchor():
    start = datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert advance_months(start, interval_months("triennially"), 29) == datetime(2029, 2, 28, tzinfo=timezone.utc)
    assert advance_months(datetime(2024, 2, 29, tzinfo=timezone.utc), 24, 29) == datetime(2026, 2, 28, tzinfo=timezone.utc)


def test_next_due_at_refuses_an_unknown_label_instead_of_billing_monthly():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert CommerceRecurringService.next_due_at(now, "biennially") == datetime(2028, 1, 15, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        CommerceRecurringService.next_due_at(now, "typo")


# ------------------------------------------------------------------ resolve_cycle


def test_resolve_by_unit_and_count_for_existing_labels(db_session):
    plan = _plan(db_session)
    _cycle(db_session, plan, "monthly", 1000)
    _cycle(db_session, plan, "annually", 10000, setup_cents=0)
    by_label = resolve_cycle(plan, "annually")
    by_unit = resolve_cycle(plan, interval_unit="year", interval_count=1)
    assert by_unit == by_label
    assert (by_unit.label, by_unit.months, by_unit.setup_cents) == ("annually", 12, 0)
    twelve_months = resolve_cycle(plan, interval_unit="month", interval_count=12)
    assert twelve_months == by_label
    assert resolve_cycle(plan, interval_unit="month", interval_count=1).setup_cents == 100
    assert resolve_cycle(plan, "monthly", interval_unit="month", interval_count=1).label == "monthly"


def test_resolve_by_unit_and_count_errors(db_session):
    plan = _plan(db_session)
    _cycle(db_session, plan, "monthly", 1000)
    with pytest.raises(CycleError, match="together"):
        resolve_cycle(plan, interval_unit="year")
    with pytest.raises(CycleError, match="Invalid"):
        resolve_cycle(plan, interval_unit="week", interval_count=1)
    with pytest.raises(CycleError, match="Invalid"):
        resolve_cycle(plan, interval_unit="month", interval_count=0)
    with pytest.raises(CycleError, match="unavailable"):
        resolve_cycle(plan, interval_unit="month", interval_count=3)
    with pytest.raises(CycleError, match="does not match"):
        resolve_cycle(plan, "monthly", interval_unit="year", interval_count=1)


def test_a_legacy_row_without_unit_and_count_matches_by_unit_and_count(db_session):
    plan = _plan(db_session)
    row = _cycle(db_session, plan, "quarterly", 3000)
    assert row.interval_unit is None
    assert resolve_cycle(plan, interval_unit="month", interval_count=3).label == "quarterly"


def test_long_cycles_are_refused_until_the_setting_is_on(db_session):
    plan = _plan(db_session)
    _cycle(db_session, plan, "monthly", 1000)
    _cycle(db_session, plan, "triennially", 27000, interval_unit="year", interval_count=3, setup_cents=50)
    _long(db_session, False)
    with pytest.raises(CycleError, match="Invalid"):
        resolve_cycle(plan, "triennially")
    with pytest.raises(CycleError, match="Invalid"):
        resolve_cycle(plan, interval_unit="year", interval_count=3)
    assert [c.interval.value for c in sellable_cycles(plan)] == ["monthly"]
    _long(db_session, True)
    resolved = resolve_cycle(plan, "triennially")
    assert (resolved.label, resolved.interval_unit, resolved.interval_count, resolved.months) == (
        "triennially", "year", 3, 36,
    )
    assert (resolved.recurring_cents, resolved.setup_cents) == (27000, 50)
    assert resolve_cycle(plan, interval_unit="month", interval_count=36) == resolved
    assert [c.interval.value for c in sellable_cycles(plan)] == ["monthly", "triennially"]


def test_one_time_plan_uses_the_lowest_id_enabled_cycle(db_session):
    plan = _plan(db_session, M.ONE_TIME, setup=0)
    _cycle(db_session, plan, "monthly", 900, enabled=False)
    first = _cycle(db_session, plan, "quarterly", 1200)
    _cycle(db_session, plan, "annually", 500)
    db_session.expire(plan)
    assert resolve_cycle(plan, None).recurring_cents == first.price_cents
    assert [c.id for c in sellable_cycles(plan)] == sorted(c.id for c in plan.cycles if c.enabled)


def test_one_time_plan_without_an_enabled_cycle_is_still_priced_at_zero(db_session):
    plan = _plan(db_session, M.ONE_TIME, setup=300)
    assert (resolve_cycle(plan).recurring_cents, resolve_cycle(plan).setup_cents) == (0, 300)


# ------------------------------------------------------------------ admin API


def _login(client) -> dict:
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def commerce(monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)


def test_admin_cycles_either_form_and_long_gate(client, db_session, test_admin_user, commerce):
    headers = _login(client)
    plan = _plan(db_session)
    db_session.commit()
    url = f"/api/admin/store/plans/{plan.id}/cycles"

    label_only = client.post(url, headers=headers, json={"interval": "quarterly", "price_cents": 3000})
    assert label_only.status_code == 201, label_only.text
    body = label_only.json()
    assert (body["interval"], body["interval_unit"], body["interval_count"], body["months"]) == (
        "quarterly", "month", 3, 3,
    )
    assert body["setup_cents"] is None

    unit_only = client.post(
        url, headers=headers, json={"interval_unit": "year", "interval_count": 1, "price_cents": 9000, "setup_cents": 0}
    )
    assert unit_only.status_code == 201, unit_only.text
    assert (unit_only.json()["interval"], unit_only.json()["setup_cents"]) == ("annually", 0)

    for bad in (
        {"price_cents": 1},
        {"interval_unit": "year", "price_cents": 1},
        {"interval_unit": "month", "interval_count": 5, "price_cents": 1},
        {"interval": "monthly", "interval_unit": "year", "interval_count": 1, "price_cents": 1},
        {"interval": "monthly", "price_cents": 1, "setup_cents": -1},
    ):
        assert client.post(url, headers=headers, json=bad).status_code == 422, bad

    assert client.post(url, headers=headers, json={"interval": "quarterly", "price_cents": 1}).status_code == 409
    assert client.post(
        url, headers=headers, json={"interval_unit": "month", "interval_count": 12, "price_cents": 1}
    ).status_code == 409
    long_body = {"interval_unit": "year", "interval_count": 3, "price_cents": 20000}
    assert client.post(url, headers=headers, json=long_body).status_code == 422
    assert client.post(url, headers=headers, json={"interval": "biennially", "price_cents": 1}).status_code == 422
    quarterly_id = label_only.json()["id"]
    assert client.put(f"/api/admin/store/cycles/{quarterly_id}", headers=headers, json={"interval": "triennially"}).status_code == 422

    _long(db_session, True)
    created = client.post(url, headers=headers, json=long_body)
    assert created.status_code == 201, created.text
    assert (created.json()["interval"], created.json()["months"]) == ("triennially", 36)
    updated = client.put(
        f"/api/admin/store/cycles/{quarterly_id}", headers=headers, json={"interval_unit": "year", "interval_count": 2, "setup_cents": 75}
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["interval"], updated.json()["setup_cents"]) == ("biennially", 75)
    price_only = client.put(f"/api/admin/store/cycles/{quarterly_id}", headers=headers, json={"price_cents": 5})
    assert price_only.json()["interval"] == "biennially" and price_only.json()["price_cents"] == 5
