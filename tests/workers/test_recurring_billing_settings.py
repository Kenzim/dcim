"""The recurring billing worker reads its configuration through the settings store."""

import pytest

from app.core.config import Settings
from app.services import settings_store
from app.services.recurring_billing_service import RecurringBillingService
from app.workers import recurring_billing
from tests.conftest import TestingSessionLocal


class SpyService(RecurringBillingService):
    instances: list = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).instances.append(self)


@pytest.fixture
def spy(db_session, monkeypatch):
    SpyService.instances = []
    monkeypatch.setattr(recurring_billing, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(recurring_billing, "RecurringBillingService", SpyService)
    return SpyService


def _base():
    return Settings(database_url="sqlite://")


@pytest.mark.asyncio
async def test_nothing_stored_uses_the_static_config_unchanged(spy):
    base = _base()
    result = await recurring_billing.run_recurring_billing_iteration(owner="w1", config=base)
    assert result["processed"] == 0
    assert spy.instances[-1].config is base


@pytest.mark.asyncio
async def test_stored_grace_days_take_effect_on_the_next_iteration(spy, db_session):
    base = _base()
    await recurring_billing.run_recurring_billing_iteration(owner="w1", config=base)
    assert spy.instances[-1].config.recurring_billing_grace_days == 7

    settings_store.set_setting(db_session, "recurring_billing_grace_days", 12, None)
    settings_store.set_setting(db_session, "recurring_billing_worker_batch_size", 5, None)
    db_session.commit()
    await recurring_billing.run_recurring_billing_iteration(owner="w1", config=base)
    config = spy.instances[-1].config
    assert config.recurring_billing_grace_days == 12
    assert config.recurring_billing_worker_batch_size == 5
    assert config.recurring_billing_retry_day_offsets == (1, 3, 5)
    assert base.recurring_billing_grace_days == 7

    settings_store.reset_setting(db_session, "recurring_billing_grace_days")
    db_session.commit()
    await recurring_billing.run_recurring_billing_iteration(owner="w1", config=base)
    assert spy.instances[-1].config.recurring_billing_grace_days == 7


@pytest.mark.asyncio
async def test_unreadable_store_falls_back_to_the_static_config(spy, monkeypatch):
    base = _base()

    def boom(db, base=None):
        raise RuntimeError("table missing")

    monkeypatch.setattr(recurring_billing, "effective_config", boom)
    await recurring_billing.run_recurring_billing_iteration(owner="w1", config=base)
    assert spy.instances[-1].config is base


def test_sleep_interval_follows_the_stored_value(spy, db_session):
    base = _base()
    assert recurring_billing._current_interval_seconds(base) == 60
    settings_store.set_setting(db_session, "recurring_billing_worker_interval_seconds", 15, None)
    db_session.commit()
    assert recurring_billing._current_interval_seconds(base) == 15
