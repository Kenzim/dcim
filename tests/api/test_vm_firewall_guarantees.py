"""VM firewall API guarantees: reads write nothing, a disabled location is never written to
Proxmox, an unreadable VM config still needs the interruption confirmation, and every write
route leaves an audit row with the right actor (successful applies themselves are not audited)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models.commerce_account import SystemSetting
from app.models.commerce_audit import UserAuditEvent
from app.models.location import Location
from app.models.reseller import Reseller, ServiceBilling
from app.models.vm_firewall import (
    FirewallBaseline,
    FirewallGroup,
    FirewallIpset,
    FirewallLocationSettings,
    FirewallRule,
    ServiceFirewall,
)
from app.services.vm_firewall import service as svc
from tests.api.vm_firewall_helpers import SSH, WEB, client_user, fake_proxmox, login, ready_world

CLIENT = "/api/client"
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
def alice(client, world, db_session):
    db_session.commit()
    return login(client, world.users["alice"].username, "secret123")


def snapshot(db) -> dict:
    """Row counts of every firewall table plus the fields a write would move."""
    db.expire_all()
    counts = {
        model.__name__: db.scalar(select(func.count()).select_from(model))
        for model in (ServiceFirewall, FirewallLocationSettings, FirewallGroup, FirewallIpset, FirewallRule,
                      FirewallBaseline, UserAuditEvent, SystemSetting)
    }
    rows = db.execute(select(
        ServiceFirewall.service_id, ServiceFirewall.revision, ServiceFirewall.applied_revision, ServiceFirewall.state,
        ServiceFirewall.nic_flag, ServiceFirewall.net0_bridge, ServiceFirewall.last_checked_at,
    ).order_by(ServiceFirewall.service_id)).all()
    locations = db.execute(select(
        FirewallLocationSettings.location_id, FirewallLocationSettings.enabled, FirewallLocationSettings.readiness,
        FirewallLocationSettings.readiness_checked_at,
    ).order_by(FirewallLocationSettings.location_id)).all()
    return {"counts": counts, "rows": [tuple(r) for r in rows], "locations": [tuple(r) for r in locations]}


# Reads write nothing


def test_every_get_route_leaves_the_database_unchanged(client, monkeypatch, db_session, world, admin, alice):
    fresh = world.vm("alice")  # no firewall row yet
    opened = world.vm("alice")
    svc.get_or_create(db_session, opened.id)
    unconfigured = Location(name="fw-no-settings")  # no settings row
    db_session.add(unconfigured)
    group = svc.create_group(db_session, world.actor("alice"), name="g", rules=[SSH])
    ipset = svc.create_ipset(db_session, world.actor("alice"), name="s", entries=["198.51.100.0/24"])
    baseline = svc.create_baseline(db_session, world.staff_actor, name="b", scope_type="global", rules=[SSH])
    user = client_user(world, "resold")
    reseller_user = type(user)(username="fw-reads-res", email="fw-reads-res@example.test", is_reseller=True)
    reseller_user.set_password("panel-password")
    reseller = Reseller(user=reseller_user)
    db_session.add(reseller)
    db_session.flush()
    resold = world.vm("resold", product_code="fw-reads")
    db_session.query(ServiceBilling).filter_by(service_id=resold.id).one().reseller_id = reseller.id
    db_session.commit()
    proxmox = fake_proxmox(monkeypatch, world, [fresh, opened, resold])
    reseller_headers = login(client, "fw-reads-res", "panel-password")

    gets = [
        (alice, f"{CLIENT}/services/{fresh.id}/firewall"),
        (alice, f"{CLIENT}/services/{opened.id}/firewall"),
        (alice, f"/api/services/{fresh.id}"),
        (alice, f"{CLIENT}/firewall/presets"),
        (alice, f"{CLIENT}/firewall/groups"),
        (alice, f"{CLIENT}/firewall/groups/{group.id}"),
        (alice, f"{CLIENT}/firewall/ipsets"),
        (alice, f"{CLIENT}/firewall/ipsets/{ipset.id}"),
        (reseller_headers, f"/api/reseller-panel/services/{resold.id}/firewall"),
        (admin, f"{ADMIN}/locations"),
        (admin, f"{ADMIN}/locations/{unconfigured.id}"),
        (admin, f"{ADMIN}/locations/{world.location.id}"),
        (admin, f"{ADMIN}/services"),
        (admin, f"{ADMIN}/services?state=off&location_id={world.location.id}&q=fw"),
        (admin, f"{ADMIN}/services/{fresh.id}"),
        (admin, f"{ADMIN}/services/{opened.id}"),
        (admin, f"{ADMIN}/baselines"),
        (admin, f"{ADMIN}/baselines/{baseline.id}"),
        (admin, f"{ADMIN}/groups"),
        (admin, f"{ADMIN}/groups/{group.id}"),
        (admin, f"{ADMIN}/ipsets"),
        (admin, f"{ADMIN}/ipsets/{ipset.id}"),
        (admin, f"{ADMIN}/worker"),
    ]
    before = snapshot(db_session)
    for headers, path in gets:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (path, response.text)
        assert not db_session.new, path
        assert snapshot(db_session) == before, path
    assert db_session.get(FirewallLocationSettings, unconfigured.id) is None
    assert db_session.get(ServiceFirewall, fresh.id) is None
    assert proxmox.calls == []
    # The defaults shown for a VM with no row are the ones its first write starts from.
    view = client.get(f"{CLIENT}/services/{fresh.id}/firewall", headers=alice).json()
    assert view["revision"] == 1 and view["state"] == "off"
    assert client.put(f"{CLIENT}/services/{fresh.id}/firewall", json={"revision": 1, "policy_in": "ACCEPT"},
                      headers=alice).status_code == 200


def test_admin_location_of_an_unconfigured_location_shows_the_defaults(client, db_session, admin):
    location = Location(name="fw-defaults")
    db_session.add(location)
    db_session.commit()
    body = client.get(f"{ADMIN}/locations/{location.id}", headers=admin).json()
    assert body["enabled"] is False and body["nic_flag_on_provision"] is True
    assert body["rollout_failures"] == 0 and body["readiness"] == {"clusters": {}}


# A location whose firewall is off


@pytest.fixture
def disabled(db_session, world):
    row = db_session.get(FirewallLocationSettings, world.location.id)
    row.enabled = False
    db_session.commit()
    return world


def test_disabled_location_refuses_enabling_and_writes_nothing(client, monkeypatch, db_session, disabled, admin, alice):
    vm = disabled.vm("alice")
    db_session.commit()
    proxmox = fake_proxmox(monkeypatch, disabled, [vm])
    for headers, url in ((alice, f"{CLIENT}/services/{vm.id}/firewall"), (admin, f"{ADMIN}/services/{vm.id}")):
        for confirm in (False, True):
            response = client.put(url, json={"revision": 1, "client_enabled": True, "confirm_interruption": confirm},
                                  headers=headers)
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "not_eligible"
            assert "location_disabled" in response.json()["reasons"]
    db_session.expire_all()
    assert db_session.get(ServiceFirewall, vm.id) is None

    # Other changes are stored as preferences; nothing reaches Proxmox.
    stored = client.put(f"{CLIENT}/services/{vm.id}/firewall/rules", json={"revision": 1, "rules": [SSH]},
                        headers=alice)
    assert stored.status_code == 200 and stored.json()["available"] is False
    admin_fields = client.put(f"{ADMIN}/services/{vm.id}", json={"revision": 2, "enforce_baseline": True},
                              headers=admin)
    assert admin_fields.status_code == 200, admin_fields.text

    applied = client.post(f"{ADMIN}/services/{vm.id}/apply", headers=admin)
    assert applied.status_code == 409 and applied.json()["reasons"] == ["location_disabled"]
    assert proxmox.writes == []
    assert not [call for call in proxmox.calls if call[0] != "GET"]


# Proxmox unreachable during the interruption check


def test_unreadable_config_needs_confirmation_even_when_the_stored_flag_is_on(
    client, monkeypatch, db_session, world, alice
):
    vm = world.vm("alice")
    row = svc.get_or_create(db_session, vm.id)
    row.nic_flag = "on"
    db_session.commit()
    proxmox = fake_proxmox(monkeypatch, world, [vm], net0="virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1")
    proxmox.fail_next(503, "proxy unreachable", count=1, method="GET", path_contains="config")
    url = f"{CLIENT}/services/{vm.id}/firewall"
    refused = client.put(url, json={"revision": 1, "client_enabled": True}, headers=alice)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "interruption_confirmation_required"

    proxmox.fail_next(503, "proxy unreachable", count=1, method="GET", path_contains="config")
    ok = client.put(url, json={"revision": 1, "client_enabled": True, "confirm_interruption": True}, headers=alice)
    assert ok.status_code == 200, ok.text
    assert ok.json()["client_enabled"] is True
    assert proxmox.writes == []


# Audit rows of every write route


def _rows(db, since: int):
    db.expire_all()
    return db.query(UserAuditEvent).filter(UserAuditEvent.id > since).order_by(UserAuditEvent.id).all()


def _last_id(db) -> int:
    return db.scalar(select(func.max(UserAuditEvent.id))) or 0


def _client_writes(client, headers, vm_id):
    """Each client write route once, in order, with the audit action it must leave."""
    url = f"{CLIENT}/services/{vm_id}/firewall"
    steps = []

    def step(action, method, path, **kwargs):
        response = client.request(method, path, headers=headers, **kwargs)
        assert response.status_code in (200, 201, 204), (path, response.text)
        steps.append(action)
        return response.json() if response.status_code != 204 else None

    rev = step("firewall.enabled", "PUT", url,
               json={"revision": 1, "client_enabled": True, "confirm_interruption": True})["revision"]
    rev = step("firewall.policy_changed", "PUT", url, json={"revision": rev, "policy_in": "ACCEPT"})["revision"]
    view = step("firewall.rules_replaced", "PUT", url + "/rules", json={"revision": rev, "rules": [SSH]})
    view = step("firewall.rule_created", "POST", url + "/rules", json={"revision": view["revision"], "rule": WEB})
    ids = [r["id"] for r in view["rules"]]
    view = step("firewall.rule_updated", "PUT", url + f"/rules/{ids[1]}",
                json={"revision": view["revision"], "rule": {**WEB, "ports": "443"}})
    view = step("firewall.rules_reordered", "POST", url + "/rules/order",
                json={"revision": view["revision"], "ids": list(reversed(ids))})
    view = step("firewall.rule_deleted", "DELETE", url + f"/rules/{ids[0]}", params={"revision": view["revision"]})
    view = step("firewall.preset_applied", "POST", url + "/presets/ping", json={"revision": view["revision"]})
    group = step("firewall.group_created", "POST", f"{CLIENT}/firewall/groups", json={"name": "g", "rules": [SSH]})
    step("firewall.group_updated", "PUT", f"{CLIENT}/firewall/groups/{group['id']}", json={"name": "g2"})
    view = step("firewall.group_attached", "POST", url + f"/groups/{group['id']}", json={"revision": view["revision"]})
    view = step("firewall.group_detached", "DELETE", url + f"/groups/{group['id']}",
                params={"revision": view["revision"]})
    step("firewall.group_deleted", "DELETE", f"{CLIENT}/firewall/groups/{group['id']}")
    ipset = step("firewall.ipset_created", "POST", f"{CLIENT}/firewall/ipsets",
                 json={"name": "s", "entries": ["198.51.100.0/24"]})
    step("firewall.ipset_updated", "PUT", f"{CLIENT}/firewall/ipsets/{ipset['id']}", json={"description": "d"})
    step("firewall.ipset_deleted", "DELETE", f"{CLIENT}/firewall/ipsets/{ipset['id']}")
    step("firewall.disabled", "PUT", url, json={"revision": view["revision"], "client_enabled": False})
    return steps


def test_every_client_write_route_is_audited(client, monkeypatch, db_session, world, alice):
    vm = world.vm("alice")
    db_session.commit()
    fake_proxmox(monkeypatch, world, [vm])
    since = _last_id(db_session)
    expected = _client_writes(client, alice, vm.id)
    rows = _rows(db_session, since)
    alice_id = world.users["alice"].id
    assert [row.action for row in rows] == expected
    for row in rows:
        assert (row.actor_user_id, row.subject_user_id, row.impersonated_by) == (alice_id, alice_id, None), row.action
        assert row.event_metadata["actor"] == {"kind": "client", "user_id": alice_id}


def test_every_client_write_route_records_impersonation(client, monkeypatch, db_session, world, admin, test_admin_user):
    vm = world.vm("alice")
    db_session.commit()
    fake_proxmox(monkeypatch, world, [vm])
    minted = client.post(f"/api/admin/clients/{world.users['alice'].id}/impersonate", headers=admin)
    assert minted.status_code == 200, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['token']}"}
    since = _last_id(db_session)
    expected = _client_writes(client, headers, vm.id)
    rows = [row for row in _rows(db_session, since) if row.action.startswith("firewall.")]
    assert [row.action for row in rows] == expected
    for row in rows:
        assert row.impersonated_by == test_admin_user.id, row.action
        assert row.actor_user_id == world.users["alice"].id, row.action


def test_every_staff_write_route_is_audited(client, monkeypatch, db_session, world, admin, test_admin_user):
    vm = world.vm("alice")
    db_session.commit()
    fake_proxmox(monkeypatch, world, [vm])
    url = f"{ADMIN}/services/{vm.id}"
    since = _last_id(db_session)
    expected = []

    def step(actions, method, path, **kwargs):
        response = client.request(method, path, headers=admin, **kwargs)
        assert response.status_code in (200, 201, 204), (path, response.text)
        expected.extend(actions)
        return response.json() if response.status_code != 204 else None

    view = step(["firewall.enabled"], "PUT", url,
                json={"revision": 1, "client_enabled": True, "confirm_interruption": True})
    view = step(["firewall.managed_changed", "firewall.admin_override_changed"], "PUT", url,
                json={"revision": view["revision"], "excluded": True, "admin_overrides": {"smtp_out": "allow"}})
    view = step(["firewall.managed_changed"], "PUT", url, json={"revision": view["revision"], "excluded": False})
    view = step(["firewall.rules_replaced"], "PUT", url + "/rules", json={"revision": view["revision"], "rules": [SSH]})
    view = step(["firewall.preset_applied"], "POST", url + "/presets/ping", json={"revision": view["revision"]})
    step(["firewall.apply_requested"], "POST", url + "/apply")
    baseline = step(["firewall.baseline_created"], "POST", f"{ADMIN}/baselines",
                    json={"name": "b", "scope_type": "global", "rules": [SSH]})
    step(["firewall.baseline_updated"], "PUT", f"{ADMIN}/baselines/{baseline['id']}", json={"enabled": False})
    step(["firewall.baseline_deleted"], "DELETE", f"{ADMIN}/baselines/{baseline['id']}")
    group = step(["firewall.group_created"], "POST", f"{ADMIN}/groups", json={"name": "g", "client_selectable": True})
    step(["firewall.group_updated"], "PUT", f"{ADMIN}/groups/{group['id']}", json={"name": "g2"})
    step(["firewall.group_deleted"], "DELETE", f"{ADMIN}/groups/{group['id']}")
    ipset = step(["firewall.ipset_created"], "POST", f"{ADMIN}/ipsets",
                 json={"name": "m", "entries": ["10.0.0.0/8"], "purpose": "management"})
    step(["firewall.ipset_updated"], "PUT", f"{ADMIN}/ipsets/{ipset['id']}", json={"description": "d"})
    step(["firewall.ipset_deleted"], "DELETE", f"{ADMIN}/ipsets/{ipset['id']}")
    step(["firewall.location_updated"], "PUT", f"{ADMIN}/locations/{world.location.id}",
         json={"ipv6_allowed_prefixes": ["2001:db8::/48"]})
    step(["firewall.rollout_resumed"], "POST", f"{ADMIN}/locations/{world.location.id}/rollout/resume")

    rows = [row for row in _rows(db_session, since) if row.action.startswith("firewall.")]
    assert [row.action for row in rows] == expected
    for row in rows:
        assert row.actor_user_id == test_admin_user.id and row.impersonated_by is None, row.action
        assert row.event_metadata["actor"]["kind"] == "staff", row.action
