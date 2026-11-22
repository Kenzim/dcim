"""tax_exchange_rates_fetch (design 4.10 and 9.1): stores each month once, never overwrites one."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.config import settings
from app.core.database import Base
from app.models.billing_jobs import BILLING_JOB_NAMES, BillingJobAction, BillingJobState
from app.models.commerce_audit import UserAuditEvent
from app.models.tax import TaxExchangeRate
from app.plugin_host import kinds
from app.plugin_host.providers import ProviderUnavailable
from app.services.billing import periodic
from app.services.billing.jobs import exchange_rates_fetch, registry, runner
from app.services.tax import exchange_rates

NAME = "tax_exchange_rates_fetch"
T0 = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fx.db'}")
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = make()
    for name in BILLING_JOB_NAMES:
        db.add(BillingJobState(job=name))
    db.commit()
    db.close()
    yield make
    engine.dispose()


class Source:
    """A stand-in for the HMRC provider: answers by period, records each call."""

    def __init__(self, monkeypatch):
        self.months: dict[str, dict[str, str]] = {}
        self.calls: list[str] = []
        self.error: str | None = None
        monkeypatch.setattr(exchange_rates_fetch, "_provider_available", lambda: True)
        monkeypatch.setattr("app.plugin_host.providers.call_provider", self.call)

    def call(self, kind, ref, method, request, **_kw):
        assert (kind, ref, method) == ("currency_rates", "hmrc_exchange_rates:hmrc_monthly", "rates")
        assert request.base == "GBP"
        self.calls.append(request.period)
        if self.error:
            raise ProviderUnavailable(self.error)
        start, end = exchange_rates.period_bounds(request.period)
        rates = tuple(kinds.CurrencyRate(c, r) for c, r in sorted(self.months.get(request.period, {}).items()))
        return kinds.CurrencyRatesResult("hmrc_monthly", start, end, rates)


@pytest.fixture
def source(monkeypatch):
    return Source(monkeypatch)


def _go_live(factory, at):
    for to, when in (("dry_run", at - timedelta(days=1)), ("live", at)):
        db = factory()
        runner.change_mode(db, NAME, to, now=when, actor_user_id=None)
        db.commit()
        db.close()
        if to == "dry_run":
            runner.run_job(factory, NAME, dry_run=True, trigger="schedule", now=when)


def _run(factory, now=T0):
    return runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=now)


def _rates(factory):
    db = factory()
    try:
        return {(r.period, r.currency): r.units_per_gbp for r in db.scalars(sa.select(TaxExchangeRate))}
    finally:
        db.close()


def test_the_job_is_registered_daily_and_ships_off(factory):
    assert NAME in registry.JOBS and NAME in BILLING_JOB_NAMES
    assert periodic.CADENCE[NAME] == periodic.DAILY
    db = factory()
    assert db.get(BillingJobState, NAME).mode == "off"
    db.close()


def test_fetches_the_current_and_next_month_once(factory, source):
    source.months = {"2026-11": {"USD": "1.3396", "EUR": "1.1930"}, "2026-12": {"USD": "1.3158"}}
    _go_live(factory, T0 - timedelta(hours=1))
    result = _run(factory)
    assert result.counts == {"done": 2}
    assert sorted(source.calls) == ["2026-11", "2026-12"]
    assert _rates(factory) == {
        ("2026-11", "USD"): Decimal("1.3396"), ("2026-11", "EUR"): Decimal("1.1930"),
        ("2026-12", "USD"): Decimal("1.3158"),
    }
    db = factory()
    row = exchange_rates.stored_rate(db, "USD", "2026-12")
    assert (row.source, row.period_start, row.period_end) == ("hmrc_monthly", date(2026, 12, 1), date(2026, 12, 31))
    db.close()
    # Idempotent: both months are stored, so the next run asks nothing.
    again = _run(factory, T0 + timedelta(days=1))
    assert again.counts == {} and len(source.calls) == 2


def test_a_month_not_published_yet_is_asked_for_again(factory, source):
    source.months = {"2026-11": {"USD": "1.3396"}}
    _go_live(factory, T0 - timedelta(hours=1))
    assert _run(factory).counts == {"done": 1, "skipped": 1}
    source.months["2026-12"] = {"USD": "1.3158"}
    assert _run(factory, T0 + timedelta(days=1)).counts == {"done": 1}
    assert source.calls == ["2026-11", "2026-12", "2026-12"]
    assert ("2026-12", "USD") in _rates(factory)


def test_a_provider_failure_writes_nothing_and_is_retried(factory, source):
    source.error = "timeout"
    _go_live(factory, T0 - timedelta(hours=1))
    assert _run(factory).counts == {"failed": 2}
    assert _rates(factory) == {}
    source.error = None
    source.months = {"2026-11": {"USD": "1.3396"}, "2026-12": {"USD": "1.3158"}}
    assert _run(factory, T0 + timedelta(days=1)).counts == {"done": 2}


def test_never_overwrites_a_stored_month(factory, source):
    db = factory()
    exchange_rates.store_month(db, "2026-11", [("USD", "1.3396")], fetched_at=T0)
    exchange_rates.store_month(db, "2026-12", [("EUR", "1.2000")], fetched_at=T0)
    db.commit()
    db.close()
    source.months = {"2026-11": {"USD": "9.9999"}, "2026-12": {"USD": "1.3158", "EUR": "1.2003"}}
    _go_live(factory, T0 - timedelta(hours=1))
    result = _run(factory)
    assert source.calls == ["2026-12"], "November's USD rate is stored, so November is not fetched"
    assert result.counts == {"done": 1}
    rates = _rates(factory)
    assert rates[("2026-11", "USD")] == Decimal("1.3396")
    assert rates[("2026-12", "EUR")] == Decimal("1.2000"), "the stored EUR rate is kept"
    assert rates[("2026-12", "USD")] == Decimal("1.3158")
    db = factory()
    action = db.scalars(sa.select(BillingJobAction).where(BillingJobAction.job == NAME)).one()
    assert action.status == "needs_review"
    assert action.detail["conflicts"] == [{"currency": "EUR", "stored": "1.200000", "offered": "1.200300"}]
    audit = db.scalars(sa.select(UserAuditEvent).where(UserAuditEvent.action == "billing.tax.exchange_rate_conflict")).one()
    assert audit.resource_id == "2026-12"
    db.close()


def test_gbp_as_the_operator_currency_needs_nothing(factory, source, monkeypatch):
    monkeypatch.setattr(settings, "commerce_default_currency", "GBP")
    _go_live(factory, T0 - timedelta(hours=1))
    assert _run(factory).counts == {} and source.calls == []


def test_the_next_month_follows_the_uk_calendar(factory, source):
    # 23:30 UTC on 30 September is 00:30 BST on 1 October: the current month is October.
    now = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)
    source.months = {"2026-10": {"USD": "1.3456"}, "2026-11": {"USD": "1.3396"}}
    _go_live(factory, now - timedelta(hours=1))
    assert _run(factory, now).counts == {"done": 2}
    assert sorted(source.calls) == ["2026-10", "2026-11"]


def test_going_live_needs_the_provider(factory, monkeypatch):
    monkeypatch.setattr(exchange_rates_fetch, "_provider_available", lambda: False)
    db = factory()
    unmet = runner.live_preconditions(db, registry.JOBS[NAME])
    db.close()
    assert unmet == ["the currency_rates provider hmrc_exchange_rates:hmrc_monthly is not installed and enabled"]


def test_a_dry_run_never_calls_the_source(factory, source):
    result = runner.run_job(factory, NAME, dry_run=True, trigger="admin", now=T0)
    assert result.counts == {"planned": 2} and source.calls == []
