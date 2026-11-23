"""The VM firewall billing routes as WHMCS calls them (docs/vm-firewall.md 10.3 and 10.5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.location import Location
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service_vm import ServiceVm
from app.models.vm_firewall import FirewallLocationSettings
from app.models.commerce_audit import UserAuditEvent
from app.plugins.base import PowerState
from app.services.vm_firewall import service as svc
from tests.contract.service_shapes import STATUS
from tests.contract.shapes import BOOL, DICT, INT, LIST, STR, assert_error, assert_shape, optional

SSH = {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22"}

FIREWALL = {
    "service_id": INT,
    "available": BOOL,
    "reasons": LIST,
    "location_enabled": BOOL,
    "client_enabled": BOOL,
    "state": STR,
    "enforced": BOOL,
    "nic_flag": STR,
    "needs_interruption": BOOL,
    "policy_in": STR,
    "policy_out": STR,
    "log_level_in": STR,
    "log_level_out": STR,
    "rules": LIST,
    "groups": LIST,
    "ipsets": LIST,
    "baseline": DICT,
    "smtp_block_help": optional(STR),
    "limits": DICT,
    "suggested_presets": LIST,
    "revision": INT,
    "last_applied_at": optional(STR),
    "message": optional(STR),
}
RULE = {"id": INT, "position": INT, "kind": STR, "enabled": BOOL, "comment": optional(STR)}
GROUP = {"id": INT, "name": STR, "description": optional(STR), "scope": STR, "rules": LIST, "revision": INT}


@pytest.fixture
def ready(whmcs, db_session):
    """A VM in a location enabled for the firewall whose cluster passed readiness."""
    service = whmcs.vm()
    cluster = db_session.get(ProxmoxCluster, db_session.get(ServiceVm, service.id).proxmox_cluster_id)
    location = Location(name=f"fw-contract-{service.id}")
    db_session.add(location)
    db_session.flush()
    cluster.location_id = location.id
    db_session.add(FirewallLocationSettings(
        location_id=location.id, enabled=True, readiness={"clusters": {str(cluster.id): {"ready": True}}},
    ))
    db_session.commit()
    return service


def _view(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert_shape(body, FIREWALL)
    for rule in body["rules"]:
        assert_shape(rule, RULE, where="rule")
    return body


def test_firewall_view_shape(whmcs, ready):
    body = _view(whmcs.call("firewall", ready.id))
    assert body["available"] is True and body["revision"] == 1 and body["rules"] == []
    assert "managed" not in body and "admin_overrides" not in body


def test_firewall_of_a_vm_outside_an_enabled_location_is_unavailable(whmcs):
    body = _view(whmcs.call("firewall", whmcs.vm().id))
    assert body["available"] is False and "location_disabled" in body["reasons"]


def test_status_reports_firewall_available(whmcs, ready, monkeypatch):
    fake = MagicMock()
    fake.get_power_state = AsyncMock(return_value=PowerState.ON)

    async def _instance(db, service):
        return fake, None

    monkeypatch.setattr("app.api.billing._billing_get_plugin_instance", _instance)
    body = whmcs.call("status", ready.id).json()
    assert_shape(body, STATUS)
    assert body["firewall_available"] is True
    assert whmcs.call("status", whmcs.vm().id).json()["firewall_available"] is False


def test_firewall_update_needs_confirm_interruption_and_a_fresh_revision(whmcs, ready, monkeypatch):
    from app.api import vm_firewall_common

    async def no_proxmox(db, service):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(vm_firewall_common, "vm_firewall_client", no_proxmox)
    refused = whmcs.call("firewall_update", ready.id, body={"revision": 1, "client_enabled": True})
    assert_error(refused, 409)
    assert refused.json()["code"] == "interruption_confirmation_required"
    ok = _view(whmcs.call("firewall_update", ready.id,
                          body={"revision": 1, "client_enabled": True, "confirm_interruption": True}))
    assert ok["client_enabled"] is True and ok["revision"] == 2
    stale = whmcs.call("firewall_update", ready.id, body={"revision": 1, "policy_in": "ACCEPT"})
    assert_error(stale, 409)
    assert stale.json()["code"] == "firewall_changed" and stale.json()["current_revision"] == 2


def test_firewall_update_refuses_admin_only_fields(whmcs, ready):
    for field, value in (("managed", False), ("excluded", True), ("admin_overrides", {})):
        response = whmcs.call("firewall_update", ready.id, body={"revision": 1, field: value})
        assert response.status_code == 422, response.text


def test_rule_routes_and_preset(whmcs, ready, db_session):
    body = _view(whmcs.call("firewall_rules_replace", ready.id, body={"revision": 1, "rules": [SSH]}))
    assert [r["ports"] for r in body["rules"]] == ["22"]
    body = _view(whmcs.call("firewall_rule_create", ready.id,
                            body={"revision": 2, "rule": {**SSH, "ports": "80"}, "position": 0}))
    assert [r["ports"] for r in body["rules"]] == ["80", "22"]
    body = _view(whmcs.call("firewall_rule_delete", ready.id, rule_id=body["rules"][0]["id"],
                            query={"revision": 3}))
    assert [r["ports"] for r in body["rules"]] == ["22"]
    body = _view(whmcs.call("firewall_preset", ready.id, preset="allow_all", body={"revision": 4}))
    assert body["policy_in"] == "ACCEPT" and body["revision"] == 5
    invalid = whmcs.call("firewall_rules_replace", ready.id, body={"revision": 5, "rules": [{**SSH, "ports": "x"}]})
    assert_error(invalid, 422)
    assert invalid.json()["field"] == "rules[0].ports"
    db_session.expire_all()
    rows = db_session.query(UserAuditEvent).filter_by(resource_id=str(ready.id)).all()
    assert rows and all(row.event_metadata["actor"]["kind"] == "billing" for row in rows)
    assert all(row.event_metadata["actor"]["integration_id"] == whmcs.integration.id for row in rows)


def test_billing_view_holds_no_internal_address_or_hidden_name(whmcs, ready, db_session):
    from tests.api.vm_firewall_helpers import assert_nothing_hidden, seed_hidden_admin_objects

    seed_hidden_admin_objects(db_session, svc.Actor.system(), ready.id)
    body = _view(whmcs.call("firewall", ready.id))
    assert_nothing_hidden(body)
    assert [g["name"] for g in body["groups"]] == ["Provided by us"]
    assert_nothing_hidden(whmcs.call("firewall_groups", ready.id).json())
    saved = _view(whmcs.call("firewall_rules_replace", ready.id,
                             body={"revision": body["revision"], "rules": body["rules"]}))
    assert_nothing_hidden(saved)


def test_writes_are_refused_on_a_suspended_service(whmcs, ready, db_session):
    from app.models.service import ServiceStatus

    ready.status = ServiceStatus.SUSPENDED
    db_session.commit()
    _view(whmcs.call("firewall", ready.id))
    refused = whmcs.call("firewall_rules_replace", ready.id, body={"revision": 1, "rules": [SSH]})
    assert_error(refused, 403)
    assert refused.json()["code"] == "service_not_active"


def test_writes_need_the_vm_firewall_permission_and_reads_do_not(whmcs, ready):
    whmcs.with_permissions(ready, {"vm.firewall": False})
    _view(whmcs.call("firewall", ready.id))
    assert_error(whmcs.call("firewall_update", ready.id, body={"revision": 1, "policy_in": "ACCEPT"}), 403)
    assert_error(whmcs.call("firewall_rules_replace", ready.id, body={"revision": 1, "rules": [SSH]}), 403)
    assert_error(whmcs.call("firewall_rule_create", ready.id, body={"revision": 1, "rule": SSH}), 403)
    assert_error(whmcs.call("firewall_rule_delete", ready.id, rule_id=1, query={"revision": 1}), 403)
    assert_error(whmcs.call("firewall_preset", ready.id, preset="ssh", body={"revision": 1}), 403)
    assert whmcs.call("firewall_groups", ready.id).status_code == 200


def test_groups_lists_only_the_owners_groups(whmcs, ready, db_session):
    other = whmcs.vm()
    own = svc.create_group(db_session, svc.Actor.client(ready.owner_user_id), name="mine", rules=[SSH])
    svc.create_group(db_session, svc.Actor.client(other.owner_user_id), name="theirs", rules=[SSH])
    shared = svc.create_group(db_session, svc.Actor.system(), name="shared", client_selectable=True)
    svc.create_group(db_session, svc.Actor.system(), name="hidden")
    db_session.commit()
    response = whmcs.call("firewall_groups", ready.id)
    assert response.status_code == 200
    for group in response.json():
        assert_shape(group, GROUP, where="group")
    assert sorted(g["id"] for g in response.json()) == sorted([own.id, shared.id])


def test_hidden_groups_and_ipsets_answer_like_missing_ids(whmcs, ready, db_session):
    hidden_group = svc.create_group(db_session, svc.Actor.system(), name="internal")
    hidden_ipset = svc.create_ipset(db_session, svc.Actor.system(), name="internal", entries=["10.0.0.0/8"])
    db_session.commit()
    for hidden_rule, missing_rule in (
        ({"kind": "group", "group_id": hidden_group.id}, {"kind": "group", "group_id": 999999}),
        ({**SSH, "remote_ipset_id": hidden_ipset.id}, {**SSH, "remote_ipset_id": 999999}),
    ):
        hidden = whmcs.call("firewall_rules_replace", ready.id, body={"revision": 1, "rules": [hidden_rule]})
        missing = whmcs.call("firewall_rules_replace", ready.id, body={"revision": 1, "rules": [missing_rule]})
        assert_error(hidden, 422)
        assert hidden.json() == missing.json()


def test_link_to_a_new_owner_drops_rules_referencing_the_old_owners_objects(whmcs, ready, db_session):
    from app.dao.user_dao import UserDAO

    staff_owner = UserDAO.create(db_session, username="fw-claim-old", email="fw-claim-old@example.com")
    ready.owner_user_id = staff_owner.id
    db_session.flush()
    old = svc.Actor.client(staff_owner.id)
    ipset = svc.create_ipset(db_session, old, name="old-office", entries=["198.51.100.0/24"])
    svc.replace_rules(db_session, ready.id, old, [{**SSH, "remote_ipset_id": ipset.id}, SSH], revision=1)
    db_session.commit()

    link = whmcs.call("link", ready.id, body={"external_service_id": "fw-claim", "external_user_id": "ext-fw-claim"})
    assert link.status_code == 200, link.text
    db_session.expire_all()
    rules = svc.service_rules(db_session, ready.id)
    assert [rule.remote_ipset_id for rule in rules] == [None]
    assert db_session.query(UserAuditEvent).filter_by(action="firewall.references_removed").count() == 1


def test_another_integrations_service_is_404_on_every_firewall_route(whmcs, db_session):
    rival_vm = whmcs.rival().vm()
    calls = [
        ("firewall", {}), ("firewall_groups", {}),
        ("firewall_update", {"body": {"revision": 1}}),
        ("firewall_rules_replace", {"body": {"revision": 1, "rules": [SSH]}}),
        ("firewall_rule_create", {"body": {"revision": 1, "rule": SSH}}),
        ("firewall_rule_delete", {"rule_id": 1, "query": {"revision": 1}}),
        ("firewall_preset", {"preset": "ssh", "body": {"revision": 1}}),
    ]
    for key, kwargs in calls:
        response = whmcs.call(key, rival_vm.id, **kwargs)
        assert_error(response, 404)
        assert response.json()["detail"] == "Service not found"
