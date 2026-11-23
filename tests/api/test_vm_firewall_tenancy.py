"""VM firewall tenancy fixes from the B5a review: what a client sees of admin objects, saving a list
that holds staff references, ``vm.firewall`` on groups and IP sets, and suspended services."""

from __future__ import annotations

import pytest

from app.models.reseller import Reseller, ServiceBilling
from app.models.service import ServiceStatus
from app.models.user import User
from app.services.vm_firewall import service as svc
from tests.api.vm_firewall_helpers import (
    SSH,
    WEB,
    assert_nothing_hidden,
    client_user,
    login,
    ready_world,
    seed_hidden_admin_objects,
)

BASE = "/api/client/services/{}/firewall"
GROUPS = "/api/client/firewall/groups"
IPSETS = "/api/client/firewall/ipsets"


@pytest.fixture
def world(db_session):
    world = ready_world(db_session)
    client_user(world, "alice")
    client_user(world, "bob")
    return world


@pytest.fixture
def alice_vm(world, db_session):
    vm = world.vm("alice")
    db_session.commit()
    return vm


@pytest.fixture
def alice(client, world, db_session):
    db_session.commit()
    return login(client, world.users["alice"].username, "secret123")


@pytest.fixture
def bob(client, world, db_session):
    db_session.commit()
    return login(client, world.users["bob"].username, "secret123")


@pytest.fixture
def admin(client, test_admin_user):
    return login(client, "admin", "adminpassword123")


def _view(client, headers, vm_id):
    response = client.get(BASE.format(vm_id), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# What a client sees


def test_client_and_reseller_responses_hold_no_internal_address_or_hidden_name(client, db_session, world, alice_vm, alice):
    seed_hidden_admin_objects(db_session, world.staff_actor, alice_vm.id)
    view = _view(client, alice, alice_vm.id)
    assert_nothing_hidden(view)
    assert_nothing_hidden(client.get(GROUPS, headers=alice).json())
    assert_nothing_hidden(client.get(IPSETS, headers=alice).json())
    # A write answers with the same view.
    written = client.put(BASE.format(alice_vm.id) + "/rules", json={"revision": view["revision"], "rules": view["rules"]},
                         headers=alice)
    assert written.status_code == 200, written.text
    assert_nothing_hidden(written.json())

    user = User(username="fw-b5a-res", email="fw-b5a-res@example.test", is_reseller=True)
    user.set_password("panel-password")
    reseller = Reseller(user=user)
    db_session.add(reseller)
    db_session.flush()
    client_user(world, "carol", reseller_id=reseller.id)
    resold = world.vm("carol", product_code="fw-b5a")
    db_session.query(ServiceBilling).filter_by(service_id=resold.id).one().reseller_id = reseller.id
    db_session.commit()
    panel = client.get(f"/api/reseller-panel/services/{resold.id}/firewall",
                       headers=login(client, user.username, "panel-password"))
    assert panel.status_code == 200, panel.text
    assert_nothing_hidden(panel.json())


def test_baseline_shows_the_effect_of_each_rule_with_neutral_labels(client, db_session, world, alice_vm, alice):
    seed_hidden_admin_objects(db_session, world.staff_actor)
    rules = [r for r in _view(client, alice, alice_vm.id)["baseline"]["rules"] if not r["generated"]]
    assert rules[0] == {"kind": "group", "enabled": True, "comment": None, "generated": False,
                        "group_name": "Provided by us"}
    assert (rules[1]["action"], rules[1]["direction"], rules[1]["remote_label"]) == ("DROP", "in", "Addresses set by us")
    assert rules[2]["remote_label"] == "Addresses set by us"
    assert "remote_label" not in rules[3] and rules[3]["protocol"] == "icmp"
    for rule in rules:
        assert not {"id", "group_id", "remote_ipset_id", "remote_cidrs", "local_cidrs", "baseline_id"} & set(rule)


def test_staff_still_see_the_baseline_in_full(client, db_session, world, alice_vm, admin):
    seed_hidden_admin_objects(db_session, world.staff_actor)
    body = client.get(f"/api/admin/vm-firewall/services/{alice_vm.id}", headers=admin).json()
    rules = body["baseline"]["rules"]
    assert rules[0]["group_name"] == "ops-hidden-group" and rules[1]["remote_cidrs"] == ["10.77.0.0/16"]


def test_selectable_admin_group_and_own_group_are_shown_in_full(client, db_session, world, alice_vm, alice):
    offered = svc.create_group(db_session, world.staff_actor, name="offered", rules=[WEB], client_selectable=True)
    own = svc.create_group(db_session, world.actor("alice"), name="mine", rules=[SSH])
    row = svc.get_or_create(db_session, alice_vm.id)
    svc.attach_group(db_session, alice_vm.id, world.actor("alice"), offered.id, revision=row.revision)
    svc.attach_group(db_session, alice_vm.id, world.actor("alice"), own.id, revision=row.revision)
    db_session.commit()
    groups = {g["name"]: g for g in _view(client, alice, alice_vm.id)["groups"]}
    assert [r["ports"] for r in groups["offered"]["rules"]] == ["80"]
    assert [r["ports"] for r in groups["mine"]["rules"]] == ["22"]


def test_hidden_group_attached_by_staff_is_provided_by_us(client, db_session, world, alice_vm, alice):
    hidden = seed_hidden_admin_objects(db_session, world.staff_actor, alice_vm.id)["group"]
    view = _view(client, alice, alice_vm.id)
    assert view["groups"] == [{"id": hidden.id, "name": "Provided by us", "description": None,
                               "provided_by_us": True, "rules": []}]


def _resold_vm(client, db_session, world):
    """A VM of a reseller's client, and the reseller's panel headers."""
    user = User(username="fw-b5c-res", email="fw-b5c-res@example.test", is_reseller=True)
    user.set_password("panel-password")
    reseller = Reseller(user=user)
    db_session.add(reseller)
    db_session.flush()
    client_user(world, "carol", reseller_id=reseller.id)
    resold = world.vm("carol", product_code="fw-b5c")
    db_session.query(ServiceBilling).filter_by(service_id=resold.id).one().reseller_id = reseller.id
    db_session.commit()
    return resold, login(client, user.username, "panel-password")


def test_hidden_group_entry_carries_nothing_more_on_client_and_reseller_responses(client, db_session, world, alice_vm, alice):
    hidden = seed_hidden_admin_objects(db_session, world.staff_actor, alice_vm.id)["group"]
    entry = {"id": hidden.id, "name": "Provided by us", "description": None, "provided_by_us": True, "rules": []}
    view = _view(client, alice, alice_vm.id)
    assert view["groups"] == [entry]
    assert [r for r in view["rules"] if r["kind"] == "group"] == [
        {"id": view["rules"][0]["id"], "position": 0, "kind": "group", "enabled": True, "comment": None,
         "group_id": hidden.id}
    ]
    written = client.put(BASE.format(alice_vm.id) + "/rules", json={"revision": view["revision"], "rules": view["rules"]},
                         headers=alice)
    assert written.status_code == 200, written.text
    assert written.json()["groups"] == [entry]

    resold, panel = _resold_vm(client, db_session, world)
    row = svc.get_or_create(db_session, resold.id)
    svc.attach_group(db_session, resold.id, world.staff_actor, hidden.id, revision=row.revision)
    db_session.commit()
    response = client.get(f"/api/reseller-panel/services/{resold.id}/firewall", headers=panel)
    assert response.status_code == 200, response.text
    assert response.json()["groups"] == [entry]


def test_hidden_group_id_answers_like_a_missing_id_on_every_client_route(client, db_session, world, alice_vm, alice):
    hidden = seed_hidden_admin_objects(db_session, world.staff_actor, alice_vm.id)["group"]
    other_vm = world.vm("alice")
    db_session.commit()
    own = client.post(GROUPS, json={"name": "own", "rules": [SSH]}, headers=alice).json()
    missing_id = hidden.id + 100000
    here, there = BASE.format(alice_vm.id), BASE.format(other_vm.id)

    def calls(group_id):
        other = _view(client, alice, other_vm.id)["revision"]
        ref = {"kind": "group", "group_id": group_id}
        return {
            "get": client.get(f"{GROUPS}/{group_id}", headers=alice),
            "update": client.put(f"{GROUPS}/{group_id}", json={"name": "renamed"}, headers=alice),
            "delete": client.delete(f"{GROUPS}/{group_id}", params={"detach": True}, headers=alice),
            "nest in a new group": client.post(GROUPS, json={"name": "nested", "rules": [ref]}, headers=alice),
            "nest in own group": client.put(f"{GROUPS}/{own['id']}", json={"rules": [ref]}, headers=alice),
            "attach on another VM": client.post(f"{there}/groups/{group_id}", json={"revision": other}, headers=alice),
            "detach on another VM": client.delete(f"{there}/groups/{group_id}", params={"revision": other},
                                                  headers=alice),
            "rules on another VM": client.put(f"{there}/rules", json={"revision": other, "rules": [ref]}, headers=alice),
            "add rule on another VM": client.post(f"{there}/rules", json={"revision": other, "rule": ref}, headers=alice),
        }

    before = _view(client, alice, alice_vm.id)
    hidden_calls, missing_calls = calls(hidden.id), calls(missing_id)
    for name, response in hidden_calls.items():
        missing = missing_calls[name]
        assert response.status_code >= 400, (name, response.text)
        assert (response.status_code, response.json()) == (missing.status_code, missing.json()), name
    assert hidden_calls["rules on another VM"].json()["code"] == "reference_not_found"
    # On its own VM the id is already in the view, and a second reference says only that.
    again = client.post(f"{here}/groups/{hidden.id}", json={"revision": before["revision"]}, headers=alice)
    assert again.status_code == 422 and again.json()["code"] == "group_already_attached"
    assert _view(client, alice, alice_vm.id) == before
    assert _view(client, alice, other_vm.id)["rules"] == []


# Saving a list that holds staff references


def test_client_can_save_its_list_with_staff_references_but_not_add_new_ones(client, db_session, world, alice_vm, alice):
    seeded = seed_hidden_admin_objects(db_session, world.staff_actor, alice_vm.id)
    other = svc.create_group(db_session, world.staff_actor, name="other-hidden", rules=[WEB])
    db_session.commit()
    url = BASE.format(alice_vm.id) + "/rules"
    view = _view(client, alice, alice_vm.id)
    saved = client.put(url, json={"revision": view["revision"], "rules": view["rules"] + [WEB]}, headers=alice)
    assert saved.status_code == 200, saved.text
    rules = saved.json()["rules"]
    assert [r.get("group_id") for r in rules[:1]] == [seeded["group"].id]
    assert rules[1]["remote_ipset_id"] == seeded["ipset"].id
    revision = saved.json()["revision"]

    added = client.put(url, json={"revision": revision, "rules": rules + [{"kind": "group", "group_id": other.id}]},
                       headers=alice)
    assert added.status_code == 422 and added.json()["code"] == "reference_not_found"
    copied = client.put(url, json={"revision": revision, "rules": rules + [{**SSH, "ports": "23",
                                                                            "remote_ipset_id": seeded["ipset"].id}]},
                        headers=alice)
    assert copied.status_code == 422 and copied.json()["field"] == "rules[3].remote_ipset_id"
    repeated = client.put(url, json={"revision": revision, "rules": rules + [{**rules[1], "ports": "23"}]}, headers=alice)
    assert repeated.status_code == 422 and repeated.json()["field"] == "rules[3].remote_ipset_id"
    added_one = client.post(BASE.format(alice_vm.id) + "/rules", json={"revision": revision, "rule": SSH}, headers=alice)
    assert added_one.status_code == 200, added_one.text


def test_foreign_group_left_after_an_ownership_change_is_neutral_and_raised_to_staff(
    client, db_session, world, alice_vm, alice, bob, admin
):
    group = svc.create_group(db_session, world.actor("alice"), name="alice-private", rules=[SSH])
    row = svc.get_or_create(db_session, alice_vm.id)
    svc.attach_group(db_session, alice_vm.id, world.actor("alice"), group.id, revision=row.revision)
    alice_vm.owner_user_id = world.users["bob"].id  # as if the ownership change left the reference
    db_session.commit()
    view = _view(client, bob, alice_vm.id)
    assert view["groups"] == [{"id": group.id, "name": "Provided by us", "description": None,
                               "provided_by_us": True, "rules": []}]
    assert "alice-private" not in str(view) and "foreign_reference" not in view["reasons"]
    staff = client.get(f"/api/admin/vm-firewall/services/{alice_vm.id}", headers=admin).json()
    assert "foreign_reference" in staff["reasons"]
    assert staff["foreign_reference_rule_ids"] == [view["rules"][0]["id"]]


# vm.firewall on groups and IP sets


def test_group_and_ipset_edits_need_vm_firewall_on_every_vm_they_reach(client, db_session, world, alice_vm, alice):
    group = svc.create_group(db_session, world.actor("alice"), name="used", rules=[SSH])
    ipset = svc.create_ipset(db_session, world.actor("alice"), name="office", entries=["198.51.100.0/24"])
    spare = svc.create_group(db_session, world.actor("alice"), name="spare", rules=[SSH])
    row = svc.get_or_create(db_session, alice_vm.id)
    svc.attach_group(db_session, alice_vm.id, world.actor("alice"), group.id, revision=row.revision)
    svc.add_rule(db_session, alice_vm.id, world.actor("alice"), {**WEB, "remote_ipset_id": ipset.id}, revision=row.revision)
    second = world.vm("alice")  # a VM the objects do not reach
    second.permission_overrides = {"vm.firewall": False}
    alice_vm.permission_overrides = {"vm.firewall": False}
    db_session.commit()
    before = row.revision

    for response in (
        client.put(f"{GROUPS}/{group.id}", json={"rules": [WEB]}, headers=alice),
        client.put(f"{GROUPS}/{group.id}", json={"name": "renamed"}, headers=alice),
        client.delete(f"{GROUPS}/{group.id}", params={"detach": True}, headers=alice),
        client.put(f"{IPSETS}/{ipset.id}", json={"entries": ["203.0.113.0/24"]}, headers=alice),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "permission_denied"
    db_session.expire_all()
    assert svc.get_or_create(db_session, alice_vm.id).revision == before
    # Objects no VM renders, and new ones, change no firewall.
    assert client.put(f"{GROUPS}/{spare.id}", json={"rules": [WEB]}, headers=alice).status_code == 200
    assert client.post(GROUPS, json={"name": "new", "rules": [SSH]}, headers=alice).status_code == 201

    alice_vm.permission_overrides = {"vm.firewall": True}
    db_session.commit()
    assert client.put(f"{GROUPS}/{group.id}", json={"rules": [WEB]}, headers=alice).status_code == 200


# Suspended and terminated services


@pytest.mark.parametrize("state", [ServiceStatus.SUSPENDED, ServiceStatus.TERMINATED])
def test_suspended_or_terminated_service_reads_but_does_not_write(client, db_session, world, alice_vm, alice, state):
    alice_vm.status = state
    db_session.commit()
    url = BASE.format(alice_vm.id)
    assert client.get(url, headers=alice).status_code == 200
    for response in (
        client.put(url, json={"revision": 1, "policy_in": "ACCEPT"}, headers=alice),
        client.put(url + "/rules", json={"revision": 1, "rules": [SSH]}, headers=alice),
        client.post(url + "/presets/ssh", json={"revision": 1}, headers=alice),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "service_not_active"


def test_group_attached_to_a_suspended_vm_cannot_be_edited(client, db_session, world, alice_vm, alice):
    group = svc.create_group(db_session, world.actor("alice"), name="used", rules=[SSH])
    row = svc.get_or_create(db_session, alice_vm.id)
    svc.attach_group(db_session, alice_vm.id, world.actor("alice"), group.id, revision=row.revision)
    alice_vm.status = ServiceStatus.SUSPENDED
    db_session.commit()
    response = client.put(f"{GROUPS}/{group.id}", json={"rules": [WEB]}, headers=alice)
    assert response.status_code == 403 and response.json()["code"] == "permission_denied"
    alice_vm.status = ServiceStatus.TERMINATED
    db_session.commit()
    assert client.put(f"{GROUPS}/{group.id}", json={"rules": [WEB]}, headers=alice).status_code == 200
