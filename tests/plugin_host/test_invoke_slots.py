"""Slot accounting for synchronous plugin handlers (app/plugin_host/invoke.py).

The stuck plugin's handlers block on ``tests.plugin_host.gates`` until the test opens the gate,
so a handler can be abandoned (the caller stops waiting) while its code is still running.
"""

import asyncio
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.plugin_host import DeliveryStatus, PluginDelivery, PluginInstallation, PluginLog, PluginSchedule
from app.plugin_host import events, health, invoke, outbox, scheduler, ui
from app.plugin_host.common import aware, utcnow
from tests.plugin_host import gates
from tests.plugin_host.helpers import base_manifest, install, make_service, make_user, run, write_plugin

STUCK = "stuck_plugin"
HEALTHY = "healthy_plugin"
ALERT = "plugin_handlers_abandoned"
SLOTS = 4  # invoke.SLOTS_PER_PLUGIN, named here so the tests also run against code without it
ENV = {"id": "e-1", "name": "service.created", "service_id": None, "payload": {}}

STUCK_CODE = """
from tests.plugin_host import gates

def register(plugin):
    @plugin.on("service.created")
    def created(ctx, event):
        gates.hold()

    @plugin.before("service.power_requested")
    def check(ctx, event):
        gates.hold()

    @plugin.action("poke")
    def poke(ctx, request):
        gates.hold()
        return {"message": "poked"}

    @plugin.schedule("tick")
    def tick(ctx, service):
        gates.hold()
"""

HEALTHY_CODE = """
def register(plugin):
    @plugin.on("service.created")
    def created(ctx, event):
        return "fine"
"""


def stuck_manifest():
    return base_manifest(
        id=STUCK,
        name="Stuck plugin",
        permissions=["service.read", "events.veto"],
        services={"types": ["vm"], "enablement": "all"},
        events=["service.created"],
        hooks=["service.power_requested"],
        actions=[{"id": "poke", "label": "Poke", "timeout": 1}],
        schedules=[{"id": "tick", "every": 3600, "timeout": 1}],
    )


def healthy_manifest():
    return base_manifest(id=HEALTHY, name="Healthy plugin", permissions=["service.read"], events=["service.created"])


@pytest.fixture
def plugins(plugin_env, db_session, monkeypatch):
    """The stuck and the healthy plugin, installed and enabled; the gate is opened at teardown."""
    monkeypatch.setattr(health, "FAILURES_TO_OPEN", 50)  # so the circuit does not hide the slot limit
    gates.reset()
    write_plugin(plugin_env, stuck_manifest(), STUCK_CODE)
    write_plugin(plugin_env, healthy_manifest(), HEALTHY_CODE)
    stuck = install(db_session, STUCK)
    healthy = install(db_session, HEALTHY)
    yield stuck, healthy
    gates.RELEASE.set()
    settle()


def settle(seconds=10.0):
    """Wait for the handler threads and the report heartbeat; they share the test's database connection."""
    deadline = time.monotonic() + seconds
    for _ in range(2):
        for thread in getattr(invoke, "_threads", list)():
            thread.join(max(0.0, deadline - time.monotonic()))


def abandon(inst, count):
    """Start ``count`` event handlers of ``inst`` that block on the gate, and stop waiting for each."""
    for _ in range(count):
        outcome = run(invoke.invoke(inst, "event", "service.created", (ENV,), timeout=0.1))
        assert outcome.timed_out, outcome
    assert gates.entered(count)


def log_lines(db, plugin_id):
    db.expire_all()
    return [row.message for row in db.execute(select(PluginLog).where(PluginLog.plugin_id == plugin_id)).scalars()]


def failures(db, plugin_id):
    db.expire_all()
    return db.get(PluginInstallation, plugin_id).consecutive_failures


# ------------------------------------------------------------------ starvation and the limits


def test_stuck_handlers_of_one_plugin_do_not_starve_another(plugins, db_session):
    stuck, healthy = plugins
    abandon(stuck, 4)
    started = time.monotonic()
    outcome = run(invoke.invoke(healthy, "event", "service.created", (ENV,), timeout=2))
    assert outcome.ok, outcome
    assert outcome.value == "fine"
    assert time.monotonic() - started < 1


def test_a_plugin_with_every_slot_held_is_refused_at_once(plugins, db_session):
    stuck, _ = plugins
    abandon(stuck, SLOTS)
    before = failures(db_session, STUCK)
    started = time.monotonic()
    outcome = run(invoke.invoke(stuck, "event", "service.created", (ENV,), timeout=2))
    assert time.monotonic() - started < 0.5
    assert not outcome.ok and getattr(outcome, "busy", False) and not outcome.timed_out
    assert "not responding" in outcome.error
    assert not gates.entered(1, seconds=0.2)  # the handler did not run
    # Every slot is held by a call that timed out: the plugin is hung, so the refusal counts.
    assert failures(db_session, STUCK) == before + 1
    assert any("refused" in line and "still running" in line for line in log_lines(db_session, STUCK))


def test_a_refusal_while_calls_are_only_in_flight_is_not_a_failure(plugins, db_session, monkeypatch):
    stuck, _ = plugins
    monkeypatch.setattr(invoke, "SLOTS_PER_PLUGIN", 1, raising=False)

    async def both():
        first = asyncio.create_task(invoke.invoke(stuck, "event", "service.created", (ENV,), timeout=5))
        await asyncio.to_thread(gates.entered, 1)
        second = await invoke.invoke(stuck, "event", "service.created", (ENV,), timeout=5)
        gates.RELEASE.set()
        return await first, second

    before = failures(db_session, STUCK)
    first, second = run(both())
    assert first.ok
    assert getattr(second, "busy", False) and "busy" in second.error
    assert failures(db_session, STUCK) == before


def test_the_global_bound_refuses_without_blaming_the_healthy_plugin(plugins, db_session, monkeypatch):
    stuck, healthy = plugins
    monkeypatch.setattr(invoke, "MAX_RUNNING", 3, raising=False)
    abandon(stuck, 3)
    outcome = run(invoke.invoke(healthy, "event", "service.created", (ENV,), timeout=1))
    assert getattr(outcome, "busy", False) and not outcome.ok
    assert failures(db_session, HEALTHY) == 0


# ------------------------------------------------------------------ each caller


def test_an_event_delivery_is_retried_later_without_using_an_attempt(plugins, db_session, monkeypatch):
    stuck, _ = plugins
    monkeypatch.setattr(outbox, "DELIVERY_TIMEOUT", 0.5)
    abandon(stuck, SLOTS)
    now = utcnow()
    row = PluginDelivery(event_id="e-2", plugin_id=STUCK, event="service.created", service_id=None, envelope=ENV,
                         status=DeliveryStatus.QUEUED, attempts=0, next_attempt_at=now, created_at=now)
    db_session.add(row)
    db_session.commit()
    run(outbox.run_once(owner="slots"))
    db_session.expire_all()
    row = db_session.get(PluginDelivery, row.id)
    assert row.status == DeliveryStatus.QUEUED
    assert row.attempts == 0
    assert aware(row.next_attempt_at) > utcnow() + timedelta(seconds=20)


def test_a_schedule_is_skipped_and_retried_later(plugins, db_session):
    stuck, _ = plugins
    abandon(stuck, SLOTS)
    counts = run(scheduler.run_once(owner="slots"))
    assert counts == {"skipped": 1}
    db_session.expire_all()
    row = db_session.execute(select(PluginSchedule).where(PluginSchedule.plugin_id == STUCK)).scalar_one()
    assert row.last_status == "skipped"
    assert row.last_run_at is None
    assert aware(row.next_run_at) < utcnow() + timedelta(seconds=120)


def test_a_before_hook_fails_open_at_once(plugins, db_session):
    stuck, _ = plugins
    owner = make_user(db_session, "slots_owner")
    service = make_service(db_session, owner)
    abandon(stuck, SLOTS)
    started = time.monotonic()
    asyncio.run(events.power_requested(db_session, service, "reboot", source="admin"))  # allowed, not vetoed
    assert time.monotonic() - started < 1  # the hook limit is 2 seconds
    assert any("refused" in line for line in log_lines(db_session, STUCK))


def test_a_ui_action_gets_a_clear_error(plugins, db_session):
    stuck, _ = plugins
    owner = make_user(db_session, "slots_owner2")
    service = make_service(db_session, owner)
    abandon(stuck, SLOTS)
    actor = {"type": "staff", "user_id": None, "via": "admin"}
    started = time.monotonic()
    with pytest.raises(ui.PluginRequestError) as info:
        run(ui.run_action(db_session, service, STUCK, "poke", params={}, confirm=False, actor=actor))
    assert time.monotonic() - started < 0.5
    assert info.value.status_code == 503
    assert "not responding" in info.value.detail


# ------------------------------------------------------------------ freeing a slot


def test_a_returning_abandoned_handler_frees_its_slot_and_closes_its_session(plugins, db_session, monkeypatch):
    stuck, _ = plugins
    sessions = []
    real_session = invoke.new_session

    def tracked_session():
        session = real_session()
        real_close = session.close

        def close():
            session.info["closed"] = True
            real_close()

        session.close = close
        sessions.append(session)
        return session

    monkeypatch.setattr(invoke, "new_session", tracked_session)
    abandon(stuck, 1)
    assert getattr(invoke, "_running", {}).get(STUCK) == 1
    assert getattr(invoke, "_abandoned", {}).get(STUCK) == 1
    assert not sessions[-1].info.get("closed")
    gates.RELEASE.set()
    settle()
    assert not getattr(invoke, "_running", {}).get(STUCK)
    assert sessions[-1].info.get("closed")
    assert STUCK not in getattr(invoke, "_abandoned", {})
    assert any("returned" in line for line in log_lines(db_session, STUCK))
    assert run(invoke.invoke(stuck, "event", "service.created", (ENV,), timeout=2)).ok


# ------------------------------------------------------------------ the alert


def test_the_alert_fires_while_handlers_are_abandoned_and_clears_after(plugins, db_session):
    from app.services.alerting import catalogue
    from app.services.alerting.detectors import DETECTORS
    from app.services.alerting.engine import run_cycle

    stuck, _ = plugins
    assert ALERT in DETECTORS and ALERT in catalogue.CATALOGUE, "no alert type for abandoned handlers"
    detector = DETECTORS[ALERT]
    params = catalogue.CATALOGUE[ALERT].defaults()
    catalogue.update_type_setting(db_session, ALERT, hold_down_seconds=0)
    db_session.commit()

    def findings():
        db_session.expire_all()
        return detector(db_session, params, catalogue.utcnow())

    abandon(stuck, 2)
    [finding] = findings()
    assert finding.resource_type == "plugin" and finding.resource_id == STUCK
    assert finding.details["abandoned"] == 2
    result = run_cycle(db_session, only_types=[ALERT], flush=lambda *args: None)
    assert result.opened == 1

    gates.RELEASE.set()
    settle()
    assert not getattr(invoke, "_running", {}).get(STUCK)
    assert findings() == []
    result = run_cycle(db_session, only_types=[ALERT], flush=lambda *args: None)
    assert result.resolved == 1
