"""Admin VM firewall API under /api/admin/vm-firewall (docs/vm-firewall.md 10.2)."""

from __future__ import annotations

import pytest

from app.models.commerce_audit import UserAuditEvent
from app.models.vm_firewall import FirewallLocationSettings, ServiceFirewall
from app.services.vm_firewall import service as svc
from tests.api.vm_firewall_helpers import SSH, client_user, fake_proxmox, login, ready_world, revision

ADMIN = "/api/admin/vm-firewall"


@pytest.fixture
def world(db_session):
    world = ready_world(db_session)
    client_user(world, "alice")
    return world


@pytest.fixture
def admin(client, test_admin_user):
    return login(client, "admin", "adminpassword123")


@pytest.fixture
def alice_headers(client, world, db_session):
    db_session.commit()
    return login(client, world.users["alice"].username, "secret123")


def _vm(world, db_session, **kw):
    vm = world.vm("alice", **kw)
    db_session.commit()
    return vm


def _opened(world, db_session, vm):
    """Give the VM a firewall row (as the first read or write does), committed."""
    svc.get_or_create(db_session, vm.id)
    db_session.commit()
    return vm


def test_admin_routes_refuse_clients(client, world, alice_headers, db_session):
    vm = _vm(world, db_session)
    for method, path in (("GET", "/locations"), ("GET", "/services"), ("GET", f"/services/{vm.id}"),
                         ("GET", "/baselines"), ("GET", "/groups"), ("GET", "/ipsets")):
        assert client.request(method, ADMIN + path, headers=alice_headers).status_code == 403


def test_location_list_and_detail(client, world, admin):
    items = client.get(ADMIN + "/locations", headers=admin).json()
    item = next(i for i in items if i["location_id"] == world.location.id)
    assert item["name"] == world.location.name and item["enabled"] is True and item["ready"] is True
    assert item["ipv6_allowed_prefixes_help"] == svc.IPV6_ALLOWED_PREFIXES_HELP
    assert item["readiness"]["clusters"][str(world.cluster.id)]["ready"] is True
    assert item["readiness"]["clusters"][str(world.cluster.id)]["name"] == world.cluster.name
    assert {"nic_flag_on_provision", "enforce_baseline_on_all", "smtp_block_new_vms", "anti_spoof_new_vms",
            "ipv6_allowed_prefixes", "readiness_checked_at", "clusters"} <= set(item)
    detail = client.get(ADMIN + f"/locations/{world.location.id}", headers=admin)
    assert detail.status_code == 200 and detail.json() == item
    assert client.get(ADMIN + "/locations/999999", headers=admin).status_code == 404


def test_location_switch_reaching_existing_vms_needs_confirmation(client, db_session, world, admin):
    vm = _opened(world, db_session, _vm(world, db_session))
    svc.create_baseline(db_session, world.staff_actor, name="base", scope_type="global", rules=[SSH])
    db_session.commit()
    url = ADMIN + f"/locations/{world.location.id}"
    refused = client.put(url, json={"enforce_baseline_on_all": True}, headers=admin)
    assert refused.status_code == 409
    body = refused.json()
    assert body["code"] == "confirmation_required" and body["field"] == "confirm_existing_vms"
    assert body["preview"]["changed"] == 1 and body["preview"]["service_ids"] == [vm.id]

    wrong = client.put(url, json={"enforce_baseline_on_all": True, "confirm_existing_vms": 5}, headers=admin)
    assert wrong.status_code == 409
    ok = client.put(url, json={"enforce_baseline_on_all": True, "confirm_existing_vms": 1}, headers=admin)
    assert ok.status_code == 200, ok.text
    assert ok.json()["enforce_baseline_on_all"] is True
    db_session.expire_all()
    assert db_session.get(ServiceFirewall, vm.id).rollout_after is not None
    assert db_session.query(UserAuditEvent).filter_by(action="firewall.location_updated").count() == 1


def test_enabling_a_location_needs_readiness_unless_staging(client, db_session, world, admin):
    row = db_session.get(FirewallLocationSettings, world.location.id)
    row.enabled = False
    row.readiness = None
    db_session.commit()
    url = ADMIN + f"/locations/{world.location.id}"
    refused = client.put(url, json={"enabled": True}, headers=admin)
    assert refused.status_code == 409 and refused.json()["code"] == "not_ready"
    staged = client.put(url, json={"enabled": True, "staging": True}, headers=admin)
    assert staged.status_code == 200 and staged.json()["enabled"] is True


@pytest.mark.parametrize("change", [
    {"ipv6_allowed_prefixes": ["2001:db8:1::/48"]},
    {"anti_spoof_new_vms": True},
])
def test_saving_a_rendered_location_setting_queues_its_vms(client, monkeypatch, db_session, world, admin, change):
    """Saving through the admin route bumps each VM's revision (the worker applies rows whose
    revision is ahead of applied_revision); nothing touches Proxmox and no drift check runs."""
    first = _opened(world, db_session, _vm(world, db_session))
    second = _opened(world, db_session, _vm(world, db_session))
    elsewhere_world = ready_world(db_session)
    client_user(elsewhere_world, "alice")
    elsewhere = _opened(elsewhere_world, db_session, _vm(elsewhere_world, db_session))
    fake = fake_proxmox(monkeypatch, world, [first, second, elsewhere])
    before = {vm.id: revision(db_session, vm.id) for vm in (first, second, elsewhere)}

    response = client.put(ADMIN + f"/locations/{world.location.id}", json=change, headers=admin)
    assert response.status_code == 200, response.text
    for key, value in change.items():
        assert response.json()[key] == value

    after = {vm.id: revision(db_session, vm.id) for vm in (first, second, elsewhere)}
    assert after[first.id] == before[first.id] + 1 and after[second.id] == before[second.id] + 1
    assert after[elsewhere.id] == before[elsewhere.id]
    for vm in (first, second):
        row = db_session.get(ServiceFirewall, vm.id)
        assert row.revision != row.applied_revision
        assert row.last_checked_at is None
    assert fake.calls == []


def test_saving_smtp_block_new_vms_queues_nothing(client, db_session, world, admin):
    vm = _opened(world, db_session, _vm(world, db_session))
    response = client.put(ADMIN + f"/locations/{world.location.id}", json={"smtp_block_new_vms": False},
                          headers=admin)
    assert response.status_code == 200
    assert revision(db_session, vm.id) == 1


def test_readiness_recheck_records_each_cluster(client, monkeypatch, db_session, world, admin):
    fake_proxmox(monkeypatch, world, [])
    db_session.get(FirewallLocationSettings, world.location.id).readiness = None
    db_session.commit()
    response = client.post(ADMIN + f"/locations/{world.location.id}/readiness", headers=admin)
    assert response.status_code == 200, response.text
    entry = response.json()["readiness"]["clusters"][str(world.cluster.id)]
    assert set(entry) >= {"ready", "reasons", "datacenter", "nodes", "name"}
    assert response.json()["readiness_checked_at"] is not None
    assert response.json()["clusters"][0]["readiness"]["ready"] == entry["ready"]


def test_rollout_dry_run_previews_and_the_real_run_needs_the_count(client, db_session, world, admin):
    vm = _opened(world, db_session, _vm(world, db_session))
    svc.create_baseline(db_session, world.staff_actor, name="base", scope_type="global", rules=[SSH])
    db_session.commit()
    url = ADMIN + f"/locations/{world.location.id}/rollout"
    dry = client.post(url, json={"dry_run": True}, headers=admin).json()
    assert dry["dry_run"] is True and dry["preview"]["changed"] == 1
    assert dry["preview"]["services"] == [{"id": vm.id, "name": vm.name}]
    assert dry["location"]["enforce_baseline_on_all"] is False

    refused = client.post(url, json={"dry_run": False}, headers=admin)
    assert refused.status_code == 409 and refused.json()["preview"]["changed"] == 1
    done = client.post(url, json={"dry_run": False, "confirm_existing_vms": 1}, headers=admin)
    assert done.status_code == 200 and done.json()["location"]["enforce_baseline_on_all"] is True


def test_baseline_crud(client, world, admin):
    created = client.post(ADMIN + "/baselines", json={
        "name": "mail", "scope_type": "location", "scope_id": world.location.id,
        "settings": {"smtp_out": "block"}, "rules": [SSH],
    }, headers=admin)
    assert created.status_code == 201, created.text
    baseline = created.json()
    assert baseline["scope_type"] == "location" and [r["ports"] for r in baseline["rules"]] == ["22"]
    assert [b["id"] for b in client.get(ADMIN + "/baselines", headers=admin).json()] == [baseline["id"]]
    updated = client.put(ADMIN + f"/baselines/{baseline['id']}", json={"enabled": False}, headers=admin).json()
    assert updated["enabled"] is False and updated["revision"] > baseline["revision"]
    assert client.get(ADMIN + f"/baselines/{baseline['id']}", headers=admin).json()["enabled"] is False
    assert client.delete(ADMIN + f"/baselines/{baseline['id']}", headers=admin).status_code == 204
    assert client.get(ADMIN + f"/baselines/{baseline['id']}", headers=admin).status_code == 404


def test_admin_groups_and_management_ipsets(client, world, admin, alice_headers):
    group = client.post(ADMIN + "/groups", json={"name": "ssh-in", "rules": [SSH], "client_selectable": True},
                        headers=admin).json()
    assert group["scope"] == "admin" and group["owner_user_id"] is None
    # A client-selectable admin group shows in the client's list, read only.
    assert group["id"] in [g["id"] for g in client.get("/api/client/firewall/groups", headers=alice_headers).json()]
    assert client.put(f"/api/client/firewall/groups/{group['id']}", json={"name": "x"},
                      headers=alice_headers).status_code == 404
    hidden = client.post(ADMIN + "/groups", json={"name": "hidden"}, headers=admin).json()
    assert client.get(f"/api/client/firewall/groups/{hidden['id']}", headers=alice_headers).status_code == 404
    renamed = client.put(ADMIN + f"/groups/{group['id']}", json={"name": "ssh"}, headers=admin).json()
    assert renamed["name"] == "ssh"
    admin_groups = client.get(ADMIN + "/groups", params={"scope": "admin"}, headers=admin).json()
    assert sorted(g["id"] for g in admin_groups) == sorted([group["id"], hidden["id"]])

    mgmt = client.post(ADMIN + "/ipsets", json={"name": "mgmt", "entries": ["10.0.0.0/8"], "purpose": "management"},
                       headers=admin)
    assert mgmt.status_code == 201, mgmt.text
    assert mgmt.json()["purpose"] == "management" and mgmt.json()["scope"] == "admin"
    listed = client.get(ADMIN + "/ipsets", params={"purpose": "management"}, headers=admin).json()
    assert [i["id"] for i in listed] == [mgmt.json()["id"]]
    assert client.get(ADMIN + f"/ipsets/{mgmt.json()['id']}", headers=admin).status_code == 200
    assert client.put(ADMIN + f"/ipsets/{mgmt.json()['id']}", json={"description": "d"},
                      headers=admin).json()["description"] == "d"
    assert client.delete(ADMIN + f"/ipsets/{mgmt.json()['id']}", headers=admin).status_code == 204
    assert client.delete(ADMIN + f"/groups/{group['id']}", headers=admin).status_code == 204


def test_staff_create_a_group_for_a_client(client, world, admin, alice_headers):
    alice = world.users["alice"]
    group = client.post(ADMIN + "/groups", json={"name": "for-alice", "owner_user_id": alice.id}, headers=admin)
    assert group.status_code == 201 and group.json()["scope"] == "client"
    mine = client.get("/api/client/firewall/groups", headers=alice_headers).json()
    assert [g["id"] for g in mine] == [group.json()["id"]]


def test_status_list_filters(client, db_session, world, admin):
    opened = _opened(world, db_session, _vm(world, db_session))
    untouched = _vm(world, db_session)
    row = db_session.get(ServiceFirewall, opened.id)
    row.state = "error"
    row.last_error = "boom"
    db_session.commit()
    rows = client.get(ADMIN + "/services", params={"location_id": world.location.id}, headers=admin).json()
    by_id = {r["service_id"]: r for r in rows}
    assert set(by_id) == {opened.id, untouched.id}
    assert by_id[opened.id]["state"] == "error" and by_id[opened.id]["last_error"] == "boom"
    assert by_id[untouched.id]["state"] == "off"
    assert by_id[opened.id]["owner_username"] == world.users["alice"].username
    assert by_id[opened.id]["location_name"] == world.location.name
    assert {"service_name", "client_enabled", "last_applied_at"} <= set(by_id[opened.id])

    errors = client.get(ADMIN + "/services", params={"state": "error"}, headers=admin).json()
    assert [r["service_id"] for r in errors] == [opened.id]
    off = client.get(ADMIN + "/services", params={"state": "off", "location_id": world.location.id},
                     headers=admin).json()
    assert [r["service_id"] for r in off] == [untouched.id]
    by_name = client.get(ADMIN + "/services", params={"q": opened.name}, headers=admin).json()
    assert [r["service_id"] for r in by_name] == [opened.id]
    by_id_q = client.get(ADMIN + "/services", params={"q": str(untouched.id)}, headers=admin).json()
    assert untouched.id in [r["service_id"] for r in by_id_q]


def test_staff_view_and_admin_fields(client, db_session, world, admin):
    vm = _vm(world, db_session)
    url = ADMIN + f"/services/{vm.id}"
    view = client.get(url, headers=admin).json()
    assert {"managed", "excluded", "enforce_baseline", "admin_overrides", "last_error", "reasons", "in_sync",
            "rendered", "ipsets"} <= set(view)
    updated = client.put(url, json={"revision": 1, "managed": False, "admin_overrides": {"smtp_out": "allow"},
                                    "policy_in": "ACCEPT"}, headers=admin)
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["managed"] is False and body["admin_overrides"] == {"smtp_out": "allow"}
    assert body["policy_in"] == "ACCEPT" and body["revision"] == 3
    db_session.expire_all()
    actions = {row.action for row in db_session.query(UserAuditEvent).filter_by(resource_id=str(vm.id))}
    assert {"firewall.managed_changed", "firewall.admin_override_changed", "firewall.policy_changed"} <= actions
    stale = client.put(url, json={"revision": 1, "excluded": True}, headers=admin)
    assert stale.status_code == 409 and stale.json()["current_revision"] == 3
    only_admin = client.put(url, json={"revision": 3, "excluded": True}, headers=admin)
    assert only_admin.status_code == 200 and only_admin.json()["excluded"] is True


def test_staff_turning_on_also_needs_confirm_interruption(client, monkeypatch, db_session, world, admin):
    vm = _vm(world, db_session)
    fake_proxmox(monkeypatch, world, [vm])
    url = ADMIN + f"/services/{vm.id}"
    refused = client.put(url, json={"revision": 1, "client_enabled": True}, headers=admin)
    assert refused.status_code == 409 and refused.json()["code"] == "interruption_confirmation_required"
    ok = client.put(url, json={"revision": 1, "client_enabled": True, "confirm_interruption": True}, headers=admin)
    assert ok.status_code == 200 and ok.json()["client_enabled"] is True


def test_staff_rule_routes(client, db_session, world, admin):
    vm = _vm(world, db_session)
    url = ADMIN + f"/services/{vm.id}"
    body = client.put(url + "/rules", json={"revision": 1, "rules": [SSH]}, headers=admin).json()
    assert [r["ports"] for r in body["rules"]] == ["22"]
    body = client.post(url + "/presets/web", json={"revision": 2}, headers=admin).json()
    assert len(body["rules"]) > 1 and body["revision"] == 3
    body = client.post(url + "/rules", json={"revision": 3, "rule": {**SSH, "ports": "2222"}}, headers=admin).json()
    ids = [r["id"] for r in body["rules"]]
    body = client.post(url + "/rules/order", json={"revision": 4, "ids": list(reversed(ids))}, headers=admin).json()
    assert [r["id"] for r in body["rules"]] == list(reversed(ids))
    body = client.put(url + f"/rules/{ids[0]}", json={"revision": 5, "rule": {**SSH, "ports": "23"}},
                      headers=admin).json()
    body = client.delete(url + f"/rules/{ids[0]}", params={"revision": 6}, headers=admin).json()
    assert ids[0] not in [r["id"] for r in body["rules"]]
    group = svc.create_group(db_session, world.staff_actor, name="g", rules=[SSH], client_selectable=True)
    db_session.commit()
    body = client.post(url + f"/groups/{group.id}", json={"revision": 7}, headers=admin).json()
    assert any(r.get("group_id") == group.id for r in body["rules"])
    body = client.delete(url + f"/groups/{group.id}", params={"revision": 8}, headers=admin).json()
    assert not any(r.get("group_id") == group.id for r in body["rules"])
    db_session.expire_all()
    staff_rows = db_session.query(UserAuditEvent).filter_by(resource_id=str(vm.id)).all()
    assert staff_rows and all(row.actor_user_id != world.users["alice"].id for row in staff_rows)


def test_apply_and_check_use_proxmox(client, monkeypatch, db_session, world, admin):
    vm = _vm(world, db_session)
    fake = fake_proxmox(monkeypatch, world, [vm])
    url = ADMIN + f"/services/{vm.id}"
    assert client.post(url + "/check", headers=admin).status_code == 404
    client.put(url, json={"revision": 1, "client_enabled": True, "confirm_interruption": True}, headers=admin)
    applied = client.post(url + "/apply", headers=admin)
    assert applied.status_code == 200, applied.text
    assert applied.json()["result"]["ok"] is True and applied.json()["firewall"]["state"] == "applied"
    assert applied.json()["firewall"]["in_sync"] is True
    assert fake.writes
    check = client.post(url + "/check", headers=admin)
    assert check.status_code == 200
    assert check.json()["in_sync"] is True and check.json()["differences"] == []


def test_apply_reports_an_unreachable_proxmox(client, monkeypatch, db_session, world, admin):
    from app.api import vm_firewall_common

    vm = _vm(world, db_session)

    async def broken(db, service):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(vm_firewall_common, "vm_firewall_client", broken)
    response = client.post(ADMIN + f"/services/{vm.id}/apply", headers=admin)
    assert response.status_code == 502 and "no route to host" in response.json()["detail"]
