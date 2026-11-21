"""Install, upgrade, settings, state, events, outbox, scheduler, hooks and health."""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.plugin_host import (
    DeliveryStatus,
    PluginDelivery,
    PluginInstallation,
    PluginSchedule,
    PluginSetting,
    PluginState,
    PluginStatus,
)
from app.models.service import ServiceStatus, ServiceType
from app.plugin_host import events, health, lifecycle, outbox, runtime, scheduler, settings_store, state
from app.plugin_host.common import utcnow
from app.plugin_host.sdk import in_window
from tests.plugin_host.helpers import (
    base_manifest,
    copy_example,
    install,
    make_service,
    make_user,
    run,
    write_plugin,
)

FLAKY_CODE = """
CALLS = []

def register(plugin):
    @plugin.on("service.created")
    def created(ctx, event):
        CALLS.append(event["service_id"])
        ctx.state(event["service_id"]).set("seen", True)
        if ctx.settings.get("fail"):
            raise RuntimeError("asked to fail")
"""


def flaky_manifest(**overrides):
    values = dict(
        permissions=["service.read"],
        services={"types": ["vm"], "enablement": "all"},
        events=["service.created"],
        settings=[{"key": "fail", "label": "Fail", "type": "boolean", "default": False}],
    )
    values.update(overrides)
    return base_manifest(**values)


def deliver_all():
    return run(outbox.run_once(owner="test-owner"))


# ------------------------------------------------------------------ lifecycle


def test_install_requires_permissions(plugin_env, db_session):
    copy_example(plugin_env)
    with pytest.raises(lifecycle.PermissionsNotAccepted) as info:
        run(lifecycle.install(db_session, "uptime_reminder", accept_permissions=["service.read"]))
    assert "notify.client" in info.value.missing
    assert db_session.get(PluginInstallation, "uptime_reminder") is None


def test_install_enable_disable_uninstall(plugin_env, db_session):
    copy_example(plugin_env)
    inst = install(db_session, "uptime_reminder")
    assert inst.status == PluginStatus.ENABLED
    assert db_session.execute(select(PluginSetting).where(PluginSetting.key == "reminder_days")).scalar_one().value == 30
    run(lifecycle.set_enabled(db_session, inst, False))
    assert inst.status == PluginStatus.DISABLED
    owner = make_user(db_session, "owner1")
    service = make_service(db_session, owner)
    state.StateView(db_session, "uptime_reminder", service_id=service.id).set("status", "ok")
    db_session.commit()
    run(lifecycle.uninstall(db_session, inst, keep_data=True))
    assert db_session.get(PluginInstallation, "uptime_reminder") is None
    assert db_session.execute(select(PluginState)).scalars().first() is not None
    inst = install(db_session, "uptime_reminder")
    run(lifecycle.uninstall(db_session, inst, keep_data=False))
    assert db_session.execute(select(PluginState)).scalars().first() is None
    assert db_session.execute(select(PluginSetting)).scalars().first() is None


def test_upgrade_shows_and_requires_new_permissions(plugin_env, db_session):
    path = write_plugin(plugin_env, flaky_manifest(), FLAKY_CODE)
    inst = install(db_session, "demo_plugin")
    manifest = flaky_manifest(version="1.1.0", permissions=["service.read", "http.outbound"])
    (path / "plugin.json").write_text(json.dumps(manifest))
    view = lifecycle.overview(db_session)["installed"][0]
    assert view["upgrade"]["version"] == "1.1.0"
    assert [p["key"] for p in view["upgrade"]["added_permissions"]] == ["http.outbound"]
    with pytest.raises(lifecycle.PermissionsNotAccepted):
        run(lifecycle.upgrade(db_session, inst, accept_permissions=[]))
    run(lifecycle.upgrade(db_session, inst, accept_permissions=["http.outbound"]))
    assert inst.version == "1.1.0" and inst.status == PluginStatus.ENABLED
    assert "http.outbound" in inst.granted_permissions
    (path / "plugin.json").write_text(json.dumps(flaky_manifest(version="1.0.5")))
    with pytest.raises(lifecycle.LifecycleError, match="Downgrades"):
        run(lifecycle.upgrade(db_session, inst, accept_permissions=[]))


def test_changed_files_stop_the_plugin(plugin_env, db_session):
    path = write_plugin(plugin_env, flaky_manifest(), FLAKY_CODE)
    inst = install(db_session, "demo_plugin")
    runtime.reset_all()
    (path / "plugin.py").write_text(FLAKY_CODE + "\n# edited\n")
    assert runtime.load_installed(inst) is None
    db_session.expire_all()
    assert db_session.get(PluginInstallation, "demo_plugin").status == PluginStatus.FILES_CHANGED
    with pytest.raises(lifecycle.LifecycleError):
        run(lifecycle.set_enabled(db_session, inst, True))
    view = lifecycle.overview(db_session)["installed"][0]
    assert view["upgrade"] is not None
    run(lifecycle.upgrade(db_session, inst, accept_permissions=[]))
    assert inst.status == PluginStatus.ENABLED


# ------------------------------------------------------------------ settings and state


def test_secret_settings_are_sealed_and_masked(plugin_env, db_session, monkeypatch):
    from cryptography.fernet import Fernet

    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "service_instance_encryption_key", Fernet.generate_key().decode())
    copy_example(plugin_env)
    install(db_session, "uptime_reminder")
    manifest = runtime.manifest_of(db_session.get(PluginInstallation, "uptime_reminder"))
    settings_store.update(db_session, "uptime_reminder", manifest, {"webhook_token": "s3cret", "reminder_days": "7"})
    db_session.commit()
    row = db_session.execute(select(PluginSetting).where(PluginSetting.key == "webhook_token")).scalar_one()
    assert row.value is None and row.sealed_value and "s3cret" not in row.sealed_value
    view = settings_store.api_view(db_session, "uptime_reminder", manifest)
    assert view["values"]["webhook_token"] == settings_store.MASK
    assert view["secrets_set"]["webhook_token"] is True
    assert view["values"]["reminder_days"] == 7
    settings_store.update(db_session, "uptime_reminder", manifest, {"webhook_token": settings_store.MASK})
    assert settings_store.values(db_session, "uptime_reminder", manifest)["webhook_token"] == "s3cret"
    settings_store.update(db_session, "uptime_reminder", manifest, {"webhook_token": ""})
    assert settings_store.values(db_session, "uptime_reminder", manifest)["webhook_token"] is None
    with pytest.raises(settings_store.SettingsError):
        settings_store.update(db_session, "uptime_reminder", manifest, {"reminder_days": 0})
    with pytest.raises(settings_store.SettingsError):
        settings_store.update(db_session, "uptime_reminder", manifest, {"nope": 1})


def test_secret_refused_without_key_when_required(plugin_env, db_session, monkeypatch):
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "service_instance_encryption_key", None)
    monkeypatch.setattr(app_settings, "require_service_instance_encryption", True)
    copy_example(plugin_env)
    install(db_session, "uptime_reminder")
    manifest = runtime.manifest_of(db_session.get(PluginInstallation, "uptime_reminder"))
    with pytest.raises(settings_store.SettingsError, match="SERVICE_INSTANCE_ENCRYPTION_KEY"):
        settings_store.update(db_session, "uptime_reminder", manifest, {"webhook_token": "x"})


def test_state_limits(db_session):
    view = state.StateView(db_session, "demo_plugin")
    view.set("a.b:c", {"x": [1, 2]})
    assert view.get("a.b:c") == {"x": [1, 2]}
    with pytest.raises(state.StateError):
        view.set("bad key", 1)
    with pytest.raises(state.StateError):
        view.set("big", "x" * (70 * 1024))
    with pytest.raises(state.StateError):
        view.set("obj", object())
    view.delete("a.b:c")
    assert view.all() == {}


def test_window():
    from datetime import datetime, timezone

    window = {"days": ["sun"], "start": "23:00", "end": "02:00", "timezone": "UTC"}
    assert in_window(window, datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc))
    assert in_window(window, datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc))
    assert not in_window(window, datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc))
    assert not in_window(window, datetime(2026, 11, 21, 23, 30, tzinfo=timezone.utc))


# ------------------------------------------------------------------ events and outbox


def test_no_plugins_means_no_rows(plugin_env, db_session):
    owner = make_user(db_session, "owner2")
    service = make_service(db_session, owner)
    service.status = ServiceStatus.SUSPENDED
    db_session.commit()
    assert db_session.execute(select(PluginDelivery)).first() is None
    assert events.emit(db_session, "order.paid", {"order_id": 1}) == 0


def test_service_lifecycle_events_reach_the_sample(plugin_env, db_session):
    copy_example(plugin_env)
    install(db_session, "uptime_reminder")
    owner = make_user(db_session, "owner3")
    service = make_service(db_session, owner)
    other = make_service(db_session, owner, service_type=ServiceType.HTTP_PROXY, name="proxy")
    rows = db_session.execute(select(PluginDelivery)).scalars().all()
    assert [(r.event, r.service_id) for r in rows] == [("service.created", service.id)]
    assert other.id not in [r.service_id for r in rows]
    assert deliver_all() == {DeliveryStatus.DELIVERED: 1}
    db_session.expire_all()
    view = state.read_service_state(db_session, "uptime_reminder", service.id)
    assert view["status"] == "ok" and view["due_at"]

    service.status = ServiceStatus.SUSPENDED
    db_session.commit()
    deliver_all()
    db_session.expire_all()
    assert state.read_service_state(db_session, "uptime_reminder", service.id)["status"] == "paused"
    service.status = ServiceStatus.ACTIVE
    db_session.commit()
    deliver_all()
    db_session.expire_all()
    assert state.read_service_state(db_session, "uptime_reminder", service.id)["status"] == "ok"
    names = [r.event for r in db_session.execute(select(PluginDelivery).order_by(PluginDelivery.id)).scalars()]
    assert names == ["service.created", "service.suspended", "service.unsuspended"]


def test_events_roll_back_with_the_change(plugin_env, db_session):
    copy_example(plugin_env)
    install(db_session, "uptime_reminder")
    owner = make_user(db_session, "owner4")
    from app.models.service import ProvisioningSource, Service

    db_session.add(Service(name="x", service_type=ServiceType.VM, status=ServiceStatus.ACTIVE,
                           owner_user_id=owner.id, provisioning_source=ProvisioningSource.INTERNAL))
    db_session.flush()
    db_session.rollback()
    assert db_session.execute(select(PluginDelivery)).first() is None


def test_commerce_events_still_feed_webhooks(plugin_env, db_session, monkeypatch):
    calls = []
    from app.services import commerce_webhook_service

    monkeypatch.setattr(commerce_webhook_service.CommerceWebhookService, "enqueue",
                        staticmethod(lambda db, name, payload: calls.append(name)))
    events.emit(db_session, "invoice.paid", {"invoice_id": 1, "billing_account_id": 2})
    events.emit(db_session, "service.created", {"service_id": 1})
    assert calls == ["invoice.paid"]


def test_failures_retry_then_dead_letter(plugin_env, db_session):
    write_plugin(plugin_env, flaky_manifest(), FLAKY_CODE)
    inst = install(db_session, "demo_plugin")
    manifest = runtime.manifest_of(inst)
    settings_store.update(db_session, "demo_plugin", manifest, {"fail": True})
    db_session.commit()
    owner = make_user(db_session, "owner5")
    service = make_service(db_session, owner)
    assert deliver_all() == {DeliveryStatus.FAILED: 1}
    db_session.expire_all()
    row = db_session.execute(select(PluginDelivery)).scalar_one()
    assert row.attempts == 1 and "asked to fail" in row.last_error
    assert state.read_service_state(db_session, "demo_plugin", service.id) == {}
    assert outbox.backoff_seconds(1) == 30 and outbox.backoff_seconds(20) == 3600
    row.attempts = outbox.MAX_ATTEMPTS - 1
    row.next_attempt_at = utcnow() - timedelta(seconds=1)
    inst = db_session.get(PluginInstallation, "demo_plugin")
    inst.circuit_open_until = None
    db_session.commit()
    assert deliver_all() == {DeliveryStatus.DEAD: 1}
    db_session.expire_all()
    assert outbox.retry(db_session, "demo_plugin", row.id)
    db_session.commit()
    assert db_session.get(PluginDelivery, row.id).status == DeliveryStatus.QUEUED


def test_circuit_opens_then_auto_disables(plugin_env, db_session):
    write_plugin(plugin_env, flaky_manifest(), FLAKY_CODE)
    install(db_session, "demo_plugin")
    for _ in range(health.FAILURES_TO_OPEN):
        health.record_failure("demo_plugin", "boom")
    db_session.expire_all()
    inst = db_session.get(PluginInstallation, "demo_plugin")
    assert health.circuit_open(inst) and health.state_of(inst) == "circuit_open"
    assert health.panel_health(inst)[0] == "unavailable"
    owner = make_user(db_session, "owner6")
    make_service(db_session, owner)
    assert deliver_all() == {}
    for _ in range(health.FAILURES_TO_DISABLE):
        health.record_failure("demo_plugin", "boom")
    db_session.expire_all()
    inst = db_session.get(PluginInstallation, "demo_plugin")
    assert inst.status == PluginStatus.FAILED and "automatically" in inst.disabled_reason
    health.record_success("demo_plugin")
    db_session.expire_all()
    assert db_session.get(PluginInstallation, "demo_plugin").consecutive_failures == 0


def test_inactive_service_is_skipped(plugin_env, db_session):
    write_plugin(plugin_env, flaky_manifest(services={"types": ["vm"], "enablement": "opt_in"}), FLAKY_CODE)
    install(db_session, "demo_plugin")
    owner = make_user(db_session, "owner7")
    make_service(db_session, owner)
    assert deliver_all() == {DeliveryStatus.SKIPPED: 1}


# ------------------------------------------------------------------ scheduler


SCHEDULE_CODE = """
def register(plugin):
    @plugin.schedule("tick")
    def tick(ctx, service):
        view = ctx.state(service["id"])
        view.set("runs", view.get("runs", 0) + 1)
"""


def test_scheduler_runs_per_service_once(plugin_env, db_session):
    manifest = base_manifest(
        permissions=["service.read"],
        services={"types": ["vm"], "enablement": "all"},
        schedules=[{"id": "tick", "every": "1h", "scope": "service"}],
    )
    write_plugin(plugin_env, manifest, SCHEDULE_CODE)
    install(db_session, "demo_plugin")
    owner = make_user(db_session, "owner8")
    service = make_service(db_session, owner)
    assert run(scheduler.run_once(owner="a")) == {"ok": 1}
    assert run(scheduler.run_once(owner="b")) == {}
    db_session.expire_all()
    assert state.read_service_state(db_session, "demo_plugin", service.id)["runs"] == 1
    row = db_session.execute(select(PluginSchedule)).scalar_one()
    assert row.last_status == "ok"
    row.next_run_at = utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert scheduler.try_lease(db_session, row.id, "first", 60)
    assert not scheduler.try_lease(db_session, row.id, "second", 60)


# ------------------------------------------------------------------ before hooks


HOOK_CODE = """
import time

def register(plugin):
    @plugin.before("service.power_requested")
    def check(ctx, event):
        mode = ctx.settings.get("mode")
        if mode == "veto" and event["payload"]["action"] == "reboot":
            return ctx.veto("Reboots are paused")
        if mode == "slow":
            time.sleep(3)
        if mode == "crash":
            raise RuntimeError("hook crashed")
        return None
"""


def hook_manifest():
    return base_manifest(
        permissions=["service.read", "events.veto"],
        services={"types": ["vm"], "enablement": "all"},
        hooks=["service.power_requested"],
        settings=[{"key": "mode", "label": "Mode", "type": "string", "default": "allow"}],
    )


@pytest.mark.parametrize("mode,vetoed", [("veto", True), ("allow", False), ("slow", False), ("crash", False)])
def test_before_hook(plugin_env, db_session, mode, vetoed):
    write_plugin(plugin_env, hook_manifest(), HOOK_CODE)
    inst = install(db_session, "demo_plugin")
    settings_store.update(db_session, "demo_plugin", runtime.manifest_of(inst), {"mode": mode})
    db_session.commit()
    owner = make_user(db_session, "owner9")
    service = make_service(db_session, owner)

    async def go():
        await events.power_requested(db_session, service, "reboot", source="admin")

    if vetoed:
        with pytest.raises(events.OperationVetoed, match="Demo plugin: Reboots are paused"):
            asyncio.run(go())
    else:
        asyncio.run(go())
    db_session.expire_all()
    failures = db_session.get(PluginInstallation, "demo_plugin").consecutive_failures
    assert failures == (1 if mode in ("slow", "crash") else 0)
