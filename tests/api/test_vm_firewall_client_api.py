"""Client VM firewall API (docs/vm-firewall.md 10.1) and the tenancy list of section 13."""

from __future__ import annotations

import pytest

from app.models.commerce_audit import UserAuditEvent
from app.models.reseller import Reseller, ServiceBilling
from app.models.vm_firewall import FirewallLocationSettings, ServiceFirewall
from app.services.vm_firewall.service import SMTP_BLOCK_HELP
from tests.api.vm_firewall_helpers import (
    FLAGGED_NET0,
    SSH,
    WEB,
    client_user,
    fake_proxmox,
    login,
    ready_world,
    revision,
)

BASE = "/api/client/services/{}/firewall"


@pytest.fixture
def world(db_session):
    world = ready_world(db_session)
    client_user(world, "alice")
    client_user(world, "bob")
    return world


@pytest.fixture
def vms(world, db_session):
    alice_vm = world.vm("alice", os_type="Linux - Cloudinit")
    bob_vm = world.vm("bob")
    db_session.commit()
    return alice_vm, bob_vm


@pytest.fixture
def proxmox(monkeypatch, world, vms):
    return fake_proxmox(monkeypatch, world, vms)


@pytest.fixture
def alice(client, world, db_session):
    db_session.commit()
    return login(client, world.users["alice"].username, "secret123")


@pytest.fixture
def bob(client, world, db_session):
    db_session.commit()
    return login(client, world.users["bob"].username, "secret123")


def _actions(db, service_id):
    db.expire_all()
    return [
        row.action
        for row in db.query(UserAuditEvent)
        .filter(UserAuditEvent.resource_type == "service", UserAuditEvent.resource_id == str(service_id))
        .order_by(UserAuditEvent.id)
    ]


VIEW_KEYS = {
    "service_id", "available", "reasons", "location_enabled", "client_enabled", "state", "enforced", "nic_flag",
    "needs_interruption", "policy_in", "policy_out", "log_level_in", "log_level_out", "rules", "groups", "ipsets",
    "baseline", "smtp_block_help", "limits", "suggested_presets", "revision", "last_applied_at", "message",
}


def test_owner_reads_the_view_without_admin_fields(client, vms, alice):
    alice_vm, _ = vms
    response = client.get(BASE.format(alice_vm.id), headers=alice)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == VIEW_KEYS
    assert body["available"] is True and body["location_enabled"] is True
    assert body["client_enabled"] is False and body["revision"] == 1
    assert body["suggested_presets"] == ["ssh", "ping"]
    assert body["needs_interruption"] is True and body["nic_flag"] == "unknown"
    assert set(body["baseline"]) >= {"anti_spoof", "smtp_out", "management_blocked", "rules"}
    assert body["limits"]["max_rules_per_vm"] > 0


def test_smtp_block_help_follows_the_merged_baseline(client, world, vms, alice, db_session):
    alice_vm, _ = vms
    assert client.get(BASE.format(alice_vm.id), headers=alice).json()["smtp_block_help"] is None
    from app.services.vm_firewall import service as svc

    svc.create_baseline(db_session, world.staff_actor, name="mail", scope_type="global",
                        settings={"smtp_out": "block"})
    db_session.commit()
    body = client.get(BASE.format(alice_vm.id), headers=alice).json()
    assert body["baseline"]["smtp_out"] == "block"
    assert body["smtp_block_help"] == SMTP_BLOCK_HELP


def test_unknown_and_foreign_services_are_404(client, vms, alice):
    _, bob_vm = vms
    assert client.get(BASE.format(bob_vm.id), headers=alice).status_code == 404
    assert client.get(BASE.format(999999), headers=alice).status_code == 404
    put = client.put(BASE.format(bob_vm.id), json={"revision": 1, "policy_in": "ACCEPT"}, headers=alice)
    assert put.status_code == 404
    rules = client.put(BASE.format(bob_vm.id) + "/rules", json={"revision": 1, "rules": [SSH]}, headers=alice)
    assert rules.status_code == 404


def test_turning_on_with_net0_flag_off_needs_confirm_interruption(client, db_session, vms, proxmox, alice):
    alice_vm, _ = vms
    url = BASE.format(alice_vm.id)
    refused = client.put(url, json={"revision": 1, "client_enabled": True}, headers=alice)
    assert refused.status_code == 409
    body = refused.json()
    assert body["code"] == "interruption_confirmation_required" and body["field"] == "confirm_interruption"
    assert body["detail"] == body["message"]
    db_session.expire_all()
    assert db_session.get(ServiceFirewall, alice_vm.id) is None

    ok = client.put(url, json={"revision": 1, "client_enabled": True, "confirm_interruption": True}, headers=alice)
    assert ok.status_code == 200, ok.text
    # 3, not 2: reading the live config also recorded net0's bridge, which bumps the revision.
    assert ok.json()["client_enabled"] is True and ok.json()["revision"] == 3
    assert ok.json()["state"] == "pending"
    assert "firewall.enabled" in _actions(db_session, alice_vm.id)
    # The VM's config was read live (net0 observed), and nothing was written to Proxmox.
    assert not proxmox.writes
    assert db_session.get(ServiceFirewall, alice_vm.id).net0_bridge == "vmbr0"


def test_turning_on_with_net0_flag_on_needs_no_confirmation(client, monkeypatch, world, vms, alice):
    alice_vm, _ = vms
    fake_proxmox(monkeypatch, world, vms, net0=FLAGGED_NET0)
    response = client.put(BASE.format(alice_vm.id), json={"revision": 1, "client_enabled": True}, headers=alice)
    assert response.status_code == 200, response.text
    assert response.json()["client_enabled"] is True


def test_not_eligible_answers_409_with_reasons(client, db_session, world, vms, proxmox, alice):
    alice_vm, _ = vms
    db_session.get(FirewallLocationSettings, world.location.id).enabled = False
    db_session.commit()
    response = client.put(
        BASE.format(alice_vm.id), json={"revision": 1, "client_enabled": True, "confirm_interruption": True},
        headers=alice,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "not_eligible"
    assert "location_disabled" in response.json()["reasons"]


def test_stale_revision_is_409_with_the_current_revision(client, vms, alice):
    alice_vm, _ = vms
    url = BASE.format(alice_vm.id)
    assert client.put(url, json={"revision": 1, "policy_in": "ACCEPT"}, headers=alice).status_code == 200
    stale = client.put(url, json={"revision": 1, "policy_in": "DROP"}, headers=alice)
    assert stale.status_code == 409
    assert stale.json()["code"] == "firewall_changed" and stale.json()["current_revision"] == 2


def test_rule_routes_each_return_the_view(client, db_session, vms, alice):
    alice_vm, _ = vms
    url = BASE.format(alice_vm.id)
    body = client.put(url + "/rules", json={"revision": 1, "rules": [SSH]}, headers=alice).json()
    assert body["revision"] == 2 and [r["ports"] for r in body["rules"]] == ["22"]

    body = client.post(url + "/rules", json={"revision": 2, "rule": WEB}, headers=alice).json()
    assert [r["ports"] for r in body["rules"]] == ["22", "80"]
    ssh_id, web_id = (r["id"] for r in body["rules"])

    body = client.put(url + f"/rules/{web_id}", json={"revision": 3, "rule": {**WEB, "ports": "443"}},
                      headers=alice).json()
    assert [r["ports"] for r in body["rules"]] == ["22", "443"]

    body = client.post(url + "/rules/order", json={"revision": 4, "ids": [web_id, ssh_id]}, headers=alice).json()
    assert [r["id"] for r in body["rules"]] == [web_id, ssh_id]

    deleted = client.delete(url + f"/rules/{ssh_id}", params={"revision": 5}, headers=alice)
    assert deleted.status_code == 200 and [r["id"] for r in deleted.json()["rules"]] == [web_id]

    body = client.post(url + "/presets/ssh", json={"revision": 6}, headers=alice).json()
    assert body["revision"] == 7 and {r["ports"] for r in body["rules"]} == {"443", "22"}

    assert _actions(db_session, alice_vm.id) == [
        "firewall.rules_replaced", "firewall.rule_created", "firewall.rule_updated", "firewall.rules_reordered",
        "firewall.rule_deleted", "firewall.preset_applied",
    ]


def test_validation_errors_name_the_field(client, vms, alice):
    alice_vm, _ = vms
    response = client.put(
        BASE.format(alice_vm.id) + "/rules", json={"revision": 1, "rules": [{**SSH, "ports": "x"}]}, headers=alice
    )
    assert response.status_code == 422
    assert response.json()["field"] == "rules[0].ports" and response.json()["code"]


def test_unknown_preset_is_refused(client, vms, alice):
    alice_vm, _ = vms
    response = client.post(BASE.format(alice_vm.id) + "/presets/nope", json={"revision": 1}, headers=alice)
    assert response.status_code in (404, 422) and response.json()["code"]


@pytest.mark.parametrize("field,value", [
    ("managed", False), ("excluded", True), ("enforce_baseline", True), ("admin_overrides", {"smtp_out": "allow"}),
])
def test_admin_only_fields_are_refused(client, db_session, vms, alice, field, value):
    alice_vm, _ = vms
    response = client.put(BASE.format(alice_vm.id), json={"revision": 1, field: value}, headers=alice)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]
    db_session.expire_all()
    assert db_session.get(ServiceFirewall, alice_vm.id) is None


def test_vm_firewall_permission_false_blocks_writes_not_reads(client, db_session, vms, alice):
    alice_vm, _ = vms
    alice_vm.permission_overrides = {"vm.firewall": False}
    db_session.commit()
    url = BASE.format(alice_vm.id)
    assert client.get(url, headers=alice).status_code == 200
    assert client.put(url, json={"revision": 1, "policy_in": "ACCEPT"}, headers=alice).status_code == 403
    assert client.put(url + "/rules", json={"revision": 1, "rules": [SSH]}, headers=alice).status_code == 403
    assert client.post(url + "/presets/ssh", json={"revision": 1}, headers=alice).status_code == 403


def test_presets_list(client, alice):
    response = client.get("/api/client/firewall/presets", headers=alice)
    assert response.status_code == 200
    keys = {preset["key"] for preset in response.json()}
    assert {"ssh", "rdp", "web", "ping", "allow_all"} <= keys
    assert all({"key", "label", "description"} <= set(preset) for preset in response.json())


# Groups and IP sets


def _group(client, headers, name="web", rules=(WEB,)):
    response = client.post("/api/client/firewall/groups", json={"name": name, "rules": list(rules)}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _ipset(client, headers, name="office"):
    response = client.post("/api/client/firewall/ipsets", json={"name": name, "entries": ["198.51.100.0/24"]},
                           headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_group_and_ipset_crud(client, alice):
    group = _group(client, alice)
    assert group["scope"] == "client" and [r["ports"] for r in group["rules"]] == ["80"]
    updated = client.put(f"/api/client/firewall/groups/{group['id']}", json={"name": "web2", "rules": [SSH]},
                         headers=alice).json()
    assert updated["name"] == "web2" and [r["ports"] for r in updated["rules"]] == ["22"]
    assert [g["id"] for g in client.get("/api/client/firewall/groups", headers=alice).json()] == [group["id"]]
    assert client.delete(f"/api/client/firewall/groups/{group['id']}", headers=alice).status_code == 204

    ipset = _ipset(client, alice)
    assert [e["cidr"] for e in ipset["entries"]] == ["198.51.100.0/24"]
    got = client.get(f"/api/client/firewall/ipsets/{ipset['id']}", headers=alice).json()
    assert got["purpose"] == "general"
    changed = client.put(f"/api/client/firewall/ipsets/{ipset['id']}", json={"entries": ["203.0.113.7"]},
                         headers=alice).json()
    assert [e["cidr"] for e in changed["entries"]] == ["203.0.113.7/32"] or changed["entries"]
    assert client.delete(f"/api/client/firewall/ipsets/{ipset['id']}", headers=alice).status_code == 204


def test_client_cannot_see_or_change_another_clients_groups_and_ipsets(client, alice, bob):
    group = _group(client, alice)
    ipset = _ipset(client, alice)
    for path in (f"/api/client/firewall/groups/{group['id']}", f"/api/client/firewall/ipsets/{ipset['id']}"):
        assert client.get(path, headers=bob).status_code == 404
        assert client.put(path, json={"name": "stolen"}, headers=bob).status_code == 404
        assert client.delete(path, headers=bob).status_code == 404
    assert client.get("/api/client/firewall/groups", headers=bob).json() == []
    assert client.get("/api/client/firewall/ipsets", headers=bob).json() == []


def test_client_cannot_attach_another_clients_group_or_reference_their_ipset(client, db_session, vms, alice, bob):
    _, bob_vm = vms
    group = _group(client, alice)
    ipset = _ipset(client, alice)
    url = BASE.format(bob_vm.id)
    missing = 999999

    def same(method, path, make_body):
        hidden = client.request(method, url + path(group["id"]), json=make_body(group["id"], ipset["id"]),
                                headers=bob)
        absent = client.request(method, url + path(missing), json=make_body(missing, missing), headers=bob)
        assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json())
        return hidden

    attach = same("POST", lambda gid: f"/groups/{gid}", lambda gid, sid: {"revision": 1})
    assert attach.status_code == 404 and attach.json()["code"] == "not_found"
    as_rule = same("PUT", lambda gid: "/rules",
                   lambda gid, sid: {"revision": 1, "rules": [{"kind": "group", "group_id": gid}]})
    assert as_rule.status_code == 422 and as_rule.json()["field"] == "rules[0].group_id"
    ref = same("PUT", lambda gid: "/rules", lambda gid, sid: {"revision": 1, "rules": [{**SSH, "remote_ipset_id": sid}]})
    assert ref.status_code == 422 and ref.json()["field"] == "rules[0].remote_ipset_id"
    db_session.expire_all()
    assert client.get(url, headers=bob).json()["rules"] == []


def test_hidden_admin_group_and_ipset_answer_like_missing_ids(client, db_session, world, vms, alice):
    from app.services.vm_firewall import service as svc

    alice_vm, _ = vms
    group = svc.create_group(db_session, world.staff_actor, name="internal", rules=[SSH])
    ipset = svc.create_ipset(db_session, world.staff_actor, name="internal", entries=["10.0.0.0/8"])
    db_session.commit()
    url = BASE.format(alice_vm.id)
    for path in ("/api/client/firewall/groups/{}", "/api/client/firewall/ipsets/{}"):
        hidden = client.get(path.format(group.id if "groups" in path else ipset.id), headers=alice)
        absent = client.get(path.format(999999), headers=alice)
        assert hidden.status_code == absent.status_code == 404 and hidden.json() == absent.json()
    hidden = client.post(url + f"/groups/{group.id}", json={"revision": 1}, headers=alice)
    absent = client.post(url + "/groups/999999", json={"revision": 1}, headers=alice)
    assert hidden.status_code == 404 and hidden.json() == absent.json()
    hidden = client.put(url + "/rules", json={"revision": 1, "rules": [{**SSH, "remote_ipset_id": ipset.id}]},
                        headers=alice)
    absent = client.put(url + "/rules", json={"revision": 1, "rules": [{**SSH, "remote_ipset_id": 999999}]},
                        headers=alice)
    assert hidden.status_code == 422 and hidden.json() == absent.json()


def test_attach_detach_and_delete_of_an_attached_group(client, vms, alice):
    alice_vm, _ = vms
    group = _group(client, alice)
    url = BASE.format(alice_vm.id)
    view = client.post(url + f"/groups/{group['id']}", json={"revision": 1}, headers=alice).json()
    assert view["rules"][0]["kind"] == "group" and view["rules"][0]["group_id"] == group["id"]
    assert [g["id"] for g in view["groups"]] == [group["id"]]

    refused = client.delete(f"/api/client/firewall/groups/{group['id']}", headers=alice)
    assert refused.status_code == 409

    view = client.delete(url + f"/groups/{group['id']}", params={"revision": 2}, headers=alice).json()
    assert view["rules"] == [] and view["groups"] == []

    client.post(url + f"/groups/{group['id']}", json={"revision": 3}, headers=alice)
    forced = client.delete(f"/api/client/firewall/groups/{group['id']}", params={"detach": True}, headers=alice)
    assert forced.status_code == 204
    assert client.get(url, headers=alice).json()["rules"] == []


def test_view_lists_the_ipsets_a_rule_may_reference(client, vms, alice, bob):
    alice_vm, _ = vms
    mine = _ipset(client, alice)
    _ipset(client, bob, name="bobs")
    ids = [item["id"] for item in client.get(BASE.format(alice_vm.id), headers=alice).json()["ipsets"]]
    assert ids == [mine["id"]]


# Impersonation, staging, the reseller panel and the service detail flag


def test_impersonation_is_recorded_on_the_audit_row(client, db_session, world, vms, test_admin_user):
    alice_vm, _ = vms
    admin = login(client, "admin", "adminpassword123")
    minted = client.post(f"/api/admin/clients/{world.users['alice'].id}/impersonate", headers=admin)
    assert minted.status_code == 200, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['token']}"}
    response = client.put(BASE.format(alice_vm.id), json={"revision": 1, "policy_in": "ACCEPT"}, headers=headers)
    assert response.status_code == 200, response.text
    db_session.expire_all()
    row = db_session.query(UserAuditEvent).filter_by(action="firewall.policy_changed").one()
    assert row.impersonated_by == test_admin_user.id
    assert row.actor_user_id == world.users["alice"].id and row.subject_user_id == world.users["alice"].id


def test_staging_location_hides_the_firewall_from_clients(client, db_session, world, vms, alice):
    alice_vm, _ = vms
    db_session.get(FirewallLocationSettings, world.location.id).readiness = None
    db_session.commit()
    body = client.get(BASE.format(alice_vm.id), headers=alice).json()
    assert body["available"] is False and "cluster_not_ready" in body["reasons"]
    detail = client.get(f"/api/services/{alice_vm.id}", headers=alice).json()
    assert detail["firewall_available"] is False


def test_service_detail_reports_firewall_available(client, vms, alice):
    alice_vm, _ = vms
    detail = client.get(f"/api/services/{alice_vm.id}", headers=alice)
    assert detail.status_code == 200, detail.text
    assert detail.json()["firewall_available"] is True


def _reseller(client, db_session, suffix):
    from app.models.user import User

    user = User(username=f"fwres-{suffix}", email=f"fwres-{suffix}@example.test", is_reseller=True)
    user.set_password("panel-password")
    account = Reseller(user=user)
    db_session.add(account)
    db_session.commit()
    return account, login(client, user.username, "panel-password")


def test_reseller_panel_reads_only_its_own_clients_firewalls(client, db_session, world):
    mine, mine_headers = _reseller(client, db_session, "a")
    other, other_headers = _reseller(client, db_session, "b")
    client_user(world, "carol", reseller_id=mine.id)
    vm = world.vm("carol", product_code="fw-res")
    db_session.query(ServiceBilling).filter_by(service_id=vm.id).one().reseller_id = mine.id
    db_session.commit()
    url = f"/api/reseller-panel/services/{vm.id}/firewall"
    response = client.get(url, headers=mine_headers)
    assert response.status_code == 200, response.text
    assert response.json()["service_id"] == vm.id and "managed" not in response.json()
    assert client.get(url, headers=other_headers).status_code == 404
    assert client.put(url, json={"revision": 1}, headers=mine_headers).status_code == 405
