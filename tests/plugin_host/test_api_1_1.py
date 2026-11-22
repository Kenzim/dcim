"""Plugin API 1.1: supported versions, effective API and the billing phase 2 events
(docs/billing-phase2-design.md 8.1 and 8.8)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.plugin_host import DeliveryStatus, PluginDelivery
from app.plugin_host import catalogue, events, outbox
from app.plugin_host.loader import discover, find_package, import_package
from app.plugin_host.manifest import effective_api, validate_manifest
from tests.plugin_host.helpers import base_manifest, install, run, write_plugin

NEW_EVENTS = {
    "tax_id.checked": {
        "billing_account_id": 1,
        "country": "DE",
        "status": "valid",
        "validator": "vies_validator:vies",
        "checked_at": "2026-11-22T12:00:00+00:00",
    },
    "invoice.reminder_sent": {"invoice_id": 1, "billing_account_id": 1, "reseller_id": None, "kind": "upcoming"},
    "service.termination_scheduled": {"service_id": 1, "terminate_at": "2026-12-01T09:00:00+00:00", "reason": "nonpayment"},
    "service.cancellation_requested": {
        "service_id": 1,
        "request_id": 7,
        "type": "end_of_period",
        "effective_at": "2026-12-31T00:00:00+00:00",
    },
}

HANDLERS = "SEEN = []\n\n\ndef register(plugin):\n" + "".join(
    f"    @plugin.on({name!r})\n    def h{i}(ctx, event):\n"
    f"        values = [event['payload'][key] for key in {sorted(payload)!r}]\n"
    f"        SEEN.append(({name!r}, values))\n"
    for i, (name, payload) in enumerate(NEW_EVENTS.items())
)


def test_constants():
    assert catalogue.API_VERSION == "1.1"
    assert catalogue.SUPPORTED_API_VERSIONS == ("1.0", "1.1")
    assert catalogue.API_VERSION == catalogue.SUPPORTED_API_VERSIONS[-1]


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("==1.0", "1.0"),
        (">=1.0,<2", "1.1"),
        (">=1.0,<2.0", "1.1"),
        (">=1.1", "1.1"),
        ("<1.1", "1.0"),
        ("==1.1", "1.1"),
        (">=1.2", None),
        (">=2.0,<3.0", None),
        ("<1.0", None),
    ],
)
def test_effective_api(spec, expected):
    assert effective_api(spec) == expected
    manifest = validate_manifest(base_manifest(rackflow_api=spec))
    assert manifest.effective_api() == expected
    assert manifest.compatible() is (expected is not None)


@pytest.mark.parametrize("spec, expected", [("==1.0", "1.0"), (">=1.0,<2", "1.1")])
def test_old_and_new_ranges_load_and_record_their_effective_api(plugin_env, db_session, spec, expected):
    write_plugin(plugin_env, base_manifest(rackflow_api=spec), "def register(plugin):\n    pass\n")
    package = find_package("demo_plugin")
    assert package.valid, package.errors
    import_package(package)
    inst = install(db_session, "demo_plugin")
    db_session.refresh(inst)
    assert inst.effective_api == expected
    assert inst.status == "enabled"


def test_a_range_allowing_no_supported_version_is_refused(plugin_env):
    write_plugin(plugin_env, base_manifest(rackflow_api=">=1.2,<2"), "def register(plugin):\n    pass\n")
    package = discover(plugin_env)[0]
    assert not package.valid
    assert "needs Rackflow plugin API >=1.2,<2; this Rackflow provides 1.0, 1.1" in package.errors


def test_the_new_events_emit_and_deliver(plugin_env, db_session):
    manifest = base_manifest(permissions=["billing.read"], events=sorted(NEW_EVENTS))
    write_plugin(plugin_env, manifest, HANDLERS)
    install(db_session, "demo_plugin")
    for name, payload in NEW_EVENTS.items():
        assert events.emit(db_session, name, payload) == 1, name
    db_session.commit()
    rows = db_session.execute(select(PluginDelivery).order_by(PluginDelivery.id)).scalars().all()
    assert [row.event for row in rows] == list(NEW_EVENTS)
    result = run(outbox.run_once(owner="test-owner"))
    db_session.expire_all()
    errors = [r.last_error for r in db_session.execute(select(PluginDelivery)).scalars()]
    assert result == {DeliveryStatus.DELIVERED: len(NEW_EVENTS)}, errors


def test_the_new_events_need_billing_read(plugin_env):
    from app.plugin_host.manifest import ManifestError

    with pytest.raises(ManifestError, match="billing.read"):
        validate_manifest(base_manifest(events=["tax_id.checked"]))
