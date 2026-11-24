"""The VM firewall domain service: revisions, tenancy, admin-only fields, revision fan-out, audit, eligibility."""

import pytest
from sqlalchemy import event

from app.core.client_permissions import (
    DEFAULT_PERMISSIONS_BY_SERVICE_TYPE,
    PERMISSION_CATALOG,
    PermissionKey,
)
from app.models.commerce_audit import UserAuditEvent as AuditLog
from app.models.service import ServiceType
from app.models.vm_firewall import FirewallLocationSettings, ServiceFirewall
from app.services.settings_store import set_setting
from app.services.vm_firewall import service as fw
from app.services.vm_firewall.errors import (
    FirewallChanged,
    FirewallConflict,
    FirewallForbidden,
    FirewallNotEligible,
    FirewallNotFound,
    FirewallValidationError,
)
from app.services.vm_firewall.service import Actor
from tests.fakes.vm_firewall_world import make_world

SSH = {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22"}
WEB = {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}


@pytest.fixture
def world(db_session):
    return make_world(db_session)


def _row(world, vm):
    return fw.get_or_create(world.db, vm.id)


def _actions(world, resource_id=None):
    query = world.db.query(AuditLog).filter(AuditLog.action.like("firewall.%"))
    if resource_id is not None:
        query = query.filter(AuditLog.resource_id == str(resource_id))
    return [entry.action for entry in query.order_by(AuditLog.id)]


# Revisions


def test_every_change_bumps_the_revision_and_stale_is_refused(world):
    vm = world.vm()
    alice = world.actor("alice")
    row = _row(world, vm)
    assert row.revision == 1
    fw.replace_rules(world.db, vm.id, alice, [SSH], revision=1)
    assert row.revision == 2
    with pytest.raises(FirewallChanged) as info:
        fw.add_rule(world.db, vm.id, alice, WEB, revision=1)
    assert (info.value.code, info.value.current_revision, info.value.http_status) == ("firewall_changed", 2, 409)
    with pytest.raises(FirewallChanged):
        fw.add_rule(world.db, vm.id, alice, WEB, revision=None)
    rule = fw.add_rule(world.db, vm.id, alice, WEB, revision=2, position=0)
    assert [r.ports for r in fw.service_rules(world.db, vm.id)] == ["80", "22"]
    fw.update_rule(world.db, vm.id, alice, rule.id, {**WEB, "ports": "8080"}, revision=3)
    fw.reorder_rules(world.db, vm.id, alice, [r.id for r in reversed(fw.service_rules(world.db, vm.id))], revision=4)
    assert [r.ports for r in fw.service_rules(world.db, vm.id)] == ["22", "8080"]
    fw.delete_rule(world.db, vm.id, alice, rule.id, revision=5)
    assert row.revision == 6 and [r.position for r in fw.service_rules(world.db, vm.id)] == [0]


def test_replace_keeps_ids_of_rules_sent_back(world):
    vm = world.vm()
    alice = world.actor("alice")
    first = fw.replace_rules(world.db, vm.id, alice, [SSH, WEB], revision=1)
    ids = [r.id for r in first]
    second = fw.replace_rules(world.db, vm.id, alice, [{**WEB, "id": ids[1]}, {**SSH, "id": ids[0]}], revision=2)
    assert [r.id for r in second] == [ids[1], ids[0]]


def test_validation_error_carries_the_field_path(world):
    vm = world.vm()
    with pytest.raises(FirewallValidationError) as info:
        fw.replace_rules(world.db, vm.id, world.actor("alice"), [SSH, {**SSH, "ports": "99999"}], revision=1)
    assert (info.value.field, info.value.code, info.value.http_status) == ("rules[1].ports", "ports_invalid", 422)
    with pytest.raises(FirewallValidationError) as info:
        fw.add_rule(world.db, vm.id, world.actor("alice"), {**SSH, "ports": "x"}, revision=1)
    assert info.value.field == "rule.ports"


def test_limits_come_from_settings(world):
    vm = world.vm()
    set_setting(world.db, "vm_firewall_max_rules_per_vm", 1)
    with pytest.raises(FirewallValidationError) as info:
        fw.replace_rules(world.db, vm.id, world.actor("alice"), [SSH, WEB], revision=1)
    assert info.value.code == "limit_exceeded"


def test_rendered_limit_counts_baseline_and_groups(world):
    vm = world.vm()
    alice = world.actor("alice")
    set_setting(world.db, "vm_firewall_max_rendered_rules", 10)
    fw.create_baseline(world.db, world.staff_actor, name="g", scope_type="global", rules=[SSH] * 8)
    group = fw.create_group(world.db, alice, name="g", rules=[WEB, SSH])
    with pytest.raises(FirewallValidationError) as info:
        fw.replace_rules(world.db, vm.id, alice, [SSH, {"kind": "group", "group_id": group.id}], revision=1)
    assert info.value.code == "rendered_limit_exceeded"


def test_logging_needs_the_setting(world):
    vm = world.vm()
    with pytest.raises(FirewallValidationError) as info:
        fw.set_firewall(world.db, vm.id, world.actor("alice"), revision=1, log_level_in="info")
    assert info.value.code == "logging_not_allowed"
    set_setting(world.db, "firewall_client_logging", True)
    fw.set_firewall(world.db, vm.id, world.actor("alice"), revision=1, log_level_in="info")


# Tenancy


def test_client_cannot_touch_another_clients_service(world):
    vm = world.vm("alice")
    with pytest.raises(FirewallNotFound):
        fw.replace_rules(world.db, vm.id, world.actor("bob"), [SSH], revision=1)
    with pytest.raises(FirewallNotFound):
        fw.get_firewall(world.db, vm.id, world.actor("bob"))


def test_client_cannot_reference_another_clients_group_or_ipset(world):
    vm = world.vm("alice")
    bob = world.actor("bob")
    group = fw.create_group(world.db, bob, name="bobs", rules=[SSH])
    ipset = fw.create_ipset(world.db, bob, name="bobs", entries=["192.0.2.0/24"])
    for rules, field in (
        ([{"kind": "group", "group_id": group.id}], "rules[0].group_id"),
        ([{**SSH, "remote_ipset_id": ipset.id}], "rules[0].remote_ipset_id"),
    ):
        with pytest.raises(FirewallValidationError) as info:
            fw.replace_rules(world.db, vm.id, world.actor("alice"), rules, revision=1)
        # Answered as for a missing id, so ids do not reveal another client's objects.
        assert (info.value.code, info.value.field) == ("reference_not_found", field)
    with pytest.raises(FirewallNotFound) as missing:
        fw.attach_group(world.db, vm.id, world.actor("alice"), group.id, revision=1)
    assert missing.value.field == "group_id"
    # Staff acting on alice's VM cannot put bob's objects there either.
    with pytest.raises(FirewallValidationError):
        fw.attach_group(world.db, vm.id, world.staff_actor, group.id, revision=1)


def test_group_references_are_checked_against_the_service_owner(world):
    """A billing actor (or staff) acting for a service is held to the service owner's objects."""
    vm = world.vm("alice")
    alice_group = fw.create_group(world.db, world.actor("alice"), name="a", rules=[SSH])
    bob_group = fw.create_group(world.db, world.actor("bob"), name="b", rules=[SSH])
    billing_for_bob = Actor.billing(integration_id=3, owner_user_id=world.user("bob").id)
    with pytest.raises(FirewallNotFound):
        fw.attach_group(world.db, vm.id, billing_for_bob, bob_group.id, revision=1)
    fw.attach_group(world.db, vm.id, world.staff_actor, alice_group.id, revision=1)
    with pytest.raises(FirewallValidationError):
        fw.attach_group(world.db, vm.id, world.staff_actor, bob_group.id, revision=2)


def _error(call):
    try:
        call()
    except (FirewallNotFound, FirewallValidationError) as exc:
        return type(exc), exc.to_dict()
    raise AssertionError("expected an error")


@pytest.mark.parametrize("kind", ["client", "billing"])
def test_hidden_admin_objects_answer_like_missing_ids(world, kind):
    vm = world.vm("alice")
    alice = world.user("alice").id
    actor = Actor.client(alice) if kind == "client" else Actor.billing(1, alice)
    group = fw.create_group(world.db, world.staff_actor, name="internal", rules=[SSH])
    ipset = fw.create_ipset(world.db, world.staff_actor, name="internal", entries=["10.0.0.0/8"])
    missing = 999_999

    def attach(group_id):
        return lambda: fw.attach_group(world.db, vm.id, actor, group_id, revision=1)

    def replace(rule):
        return lambda: fw.replace_rules(world.db, vm.id, actor, [rule], revision=1)

    hidden, absent = _error(attach(group.id)), _error(attach(missing))
    assert hidden == absent and hidden[0] is FirewallNotFound
    assert absent[1] == {"code": "not_found", "field": "group_id", "message": "Security group not found"}
    assert _error(replace({"kind": "group", "group_id": group.id})) == _error(
        replace({"kind": "group", "group_id": missing}))
    assert _error(replace({**SSH, "remote_ipset_id": ipset.id})) == _error(replace({**SSH, "remote_ipset_id": missing}))
    if kind == "client":
        assert _error(lambda: fw.get_group(world.db, actor, group.id)) == _error(
            lambda: fw.get_group(world.db, actor, missing))
        assert _error(lambda: fw.get_ipset(world.db, actor, ipset.id)) == _error(
            lambda: fw.get_ipset(world.db, actor, missing))
    # Staff may use admin objects.
    fw.attach_group(world.db, vm.id, world.staff_actor, group.id, revision=1)


def test_client_selectable_admin_objects(world):
    vm = world.vm("alice")
    offered = fw.create_group(world.db, world.staff_actor, name="monitoring", rules=[SSH], client_selectable=True)
    private = fw.create_group(world.db, world.staff_actor, name="internal", rules=[SSH])
    fw.attach_group(world.db, vm.id, world.actor("alice"), offered.id, revision=1)
    with pytest.raises(FirewallNotFound):
        fw.attach_group(world.db, vm.id, world.actor("alice"), private.id, revision=2)
    names = {g.name for g in fw.list_groups(world.db, world.actor("alice"))}
    assert names == {"monitoring"}
    with pytest.raises(FirewallForbidden):
        fw.update_group(world.db, world.actor("alice"), offered.id, rules=[])


def test_client_groups_cannot_use_admin_ipsets_that_are_not_offered(world):
    secret = fw.create_ipset(world.db, world.staff_actor, name="noc", entries=["10.0.0.0/8"])
    with pytest.raises(FirewallValidationError):
        fw.create_group(world.db, world.actor("alice"), name="g", rules=[{**SSH, "remote_ipset_id": secret.id}])


def test_ownership_change_removes_foreign_references(world):
    vm = world.vm("alice")
    group = fw.create_group(world.db, world.actor("alice"), name="a", rules=[SSH])
    fw.replace_rules(world.db, vm.id, world.actor("alice"), [WEB, {"kind": "group", "group_id": group.id}], revision=1)
    vm.owner_user_id = world.user("bob").id
    world.db.flush()
    removed = fw.remove_foreign_references(world.db, vm.id, world.staff_actor)
    assert len(removed) == 1 and [r.ports for r in fw.service_rules(world.db, vm.id)] == ["80"]


# Admin-only fields


@pytest.mark.parametrize("actor_kind", ["client", "billing"])
def test_admin_only_fields_refused_for_non_staff(world, actor_kind):
    vm = world.vm("alice")
    alice = world.user("alice")
    actor = Actor.client(alice.id) if actor_kind == "client" else Actor.billing(1, alice.id)
    calls = [
        lambda: fw.set_admin_fields(world.db, vm.id, actor, revision=1, excluded=True),
        lambda: fw.set_admin_fields(world.db, vm.id, actor, revision=1, managed=False),
        lambda: fw.set_admin_fields(world.db, vm.id, actor, revision=1, admin_overrides={"macfilter": False}),
        lambda: fw.create_baseline(world.db, actor, name="b", scope_type="global"),
        lambda: fw.update_location_settings(world.db, actor, world.location.id, enabled=False),
        lambda: fw.create_ipset(world.db, actor, name="m", purpose="management"),
        lambda: fw.create_ipset(world.db, actor, name="s", client_selectable=True),
    ]
    for call in calls:
        with pytest.raises(FirewallForbidden):
            call()
    with pytest.raises(FirewallValidationError):
        fw.create_group(world.db, actor, name="g", client_selectable=True)
    admin_group = fw.create_group(world.db, world.staff_actor, name="adm")
    with pytest.raises(FirewallNotFound):
        fw.delete_group(world.db, actor, admin_group.id)
    row = _row(world, vm)
    assert (row.excluded, row.managed, row.admin_overrides) == (False, True, None)


def test_staff_sets_admin_fields_with_audit(world):
    vm = world.vm()
    fw.set_admin_fields(world.db, vm.id, world.staff_actor, revision=1, excluded=True,
                        admin_overrides={"anti_spoof": False})
    assert _actions(world, vm.id) == ["firewall.managed_changed", "firewall.admin_override_changed"]


# Revision fan-out


def _count_updates(db):
    statements = []

    def listener(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE SERVICE_FIREWALLS"):
            statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", listener)
    return statements, lambda: event.remove(db.get_bind(), "before_cursor_execute", listener)


def test_group_change_bumps_every_vm_rendering_it_in_one_update(world):
    alice = world.actor("alice")
    group = fw.create_group(world.db, alice, name="g", rules=[SSH])
    using = [world.vm("alice") for _ in range(3)]
    other = world.vm("alice")
    for vm in using:
        fw.attach_group(world.db, vm.id, alice, group.id, revision=1)
    _row(world, other)
    world.db.flush()
    statements, stop = _count_updates(world.db)
    try:
        fw.update_group(world.db, alice, group.id, rules=[SSH, WEB])
    finally:
        stop()
    assert len(statements) == 1
    assert [world.db.get(ServiceFirewall, vm.id).revision for vm in using] == [3, 3, 3]
    assert world.db.get(ServiceFirewall, other.id).revision == 1


def test_renaming_a_group_does_not_bump_vms(world):
    alice = world.actor("alice")
    group = fw.create_group(world.db, alice, name="g", rules=[SSH])
    vm = world.vm("alice")
    fw.attach_group(world.db, vm.id, alice, group.id, revision=1)
    fw.update_group(world.db, alice, group.id, name="renamed")
    assert _row(world, vm).revision == 2 and group.revision == 2


def test_ipset_change_bumps_vms_using_it_directly_or_through_groups(world):
    alice = world.actor("alice")
    ipset = fw.create_ipset(world.db, alice, name="office", entries=["198.51.100.0/24"])
    group = fw.create_group(world.db, alice, name="g", rules=[{**SSH, "remote_ipset_id": ipset.id}])
    direct, via_group, unrelated = world.vm(), world.vm(), world.vm()
    fw.replace_rules(world.db, direct.id, alice, [{**SSH, "remote_ipset_id": ipset.id}], revision=1)
    fw.attach_group(world.db, via_group.id, alice, group.id, revision=1)
    _row(world, unrelated)
    fw.update_ipset(world.db, alice, ipset.id, entries=["198.51.100.0/24", "203.0.113.0/24"])
    assert [_row(world, v).revision for v in (direct, via_group, unrelated)] == [3, 3, 1]
    with pytest.raises(FirewallConflict):
        fw.delete_ipset(world.db, alice, ipset.id)


def test_baseline_change_bumps_vms_in_scope(world):
    in_scope = world.vm(product_code="vps")
    out_of_scope = world.vm(product_code="other")
    _row(world, in_scope), _row(world, out_of_scope)
    baseline = fw.create_baseline(world.db, world.staff_actor, name="p", scope_type="product",
                                  scope_id=world.product("vps").id)
    assert (_row(world, in_scope).revision, _row(world, out_of_scope).revision) == (2, 1)
    fw.update_baseline(world.db, world.staff_actor, baseline.id, settings={"smtp_out": "block"})
    assert (_row(world, in_scope).revision, _row(world, out_of_scope).revision) == (3, 1)
    with pytest.raises(FirewallChanged):
        fw.update_baseline(world.db, world.staff_actor, baseline.id, revision=1, enabled=False)
    fw.create_baseline(world.db, world.staff_actor, name="g", scope_type="global")
    assert (_row(world, in_scope).revision, _row(world, out_of_scope).revision) == (4, 2)


def test_management_ipset_bumps_every_vm(world):
    vm = world.vm()
    _row(world, vm)
    ipset = fw.create_ipset(world.db, world.staff_actor, name="mgmt", purpose="management", entries=["10.1.0.0/16"])
    assert _row(world, vm).revision == 2
    fw.update_ipset(world.db, world.staff_actor, ipset.id, entries=["10.2.0.0/16"])
    assert _row(world, vm).revision == 3


def test_group_in_use_needs_detach(world):
    alice = world.actor("alice")
    group = fw.create_group(world.db, alice, name="g", rules=[SSH])
    vm = world.vm()
    fw.replace_rules(world.db, vm.id, alice, [WEB, {"kind": "group", "group_id": group.id}, SSH], revision=1)
    with pytest.raises(FirewallConflict):
        fw.delete_group(world.db, alice, group.id)
    fw.delete_group(world.db, alice, group.id, detach=True)
    rules = fw.service_rules(world.db, vm.id)
    assert [(r.ports, r.position) for r in rules] == [("80", 0), ("22", 1)] and _row(world, vm).revision == 3


def test_group_and_ipset_limits_and_names(world):
    alice = world.actor("alice")
    set_setting(world.db, "vm_firewall_max_groups_per_client", 1)
    fw.create_group(world.db, alice, name="one")
    with pytest.raises(FirewallValidationError):
        fw.create_group(world.db, alice, name="two")
    fw.create_ipset(world.db, alice, name="dup")
    with pytest.raises(FirewallConflict):
        fw.create_ipset(world.db, alice, name="dup")
    fw.create_ipset(world.db, world.actor("bob"), name="dup")


# Audit


def test_mutations_write_audit_rows_with_before_and_after(world):
    vm = world.vm()
    alice = world.actor("alice")
    fw.set_firewall(world.db, vm.id, alice, revision=1, client_enabled=True, policy_in="REJECT")
    fw.replace_rules(world.db, vm.id, alice, [SSH], revision=2)
    fw.apply_preset(world.db, vm.id, alice, "web", revision=3)
    assert _actions(world, vm.id) == [
        "firewall.enabled", "firewall.policy_changed", "firewall.rules_replaced", "firewall.preset_applied",
    ]
    entry = world.db.query(AuditLog).filter_by(action="firewall.policy_changed").one()
    assert entry.subject_user_id == world.user("alice").id and entry.actor_user_id == world.user("alice").id
    meta = entry.event_metadata
    assert meta["before"]["policy_in"] == "DROP" and meta["after"]["policy_in"] == "REJECT"
    assert meta["actor"] == {"kind": "client", "user_id": world.user("alice").id}


def test_no_op_writes_no_audit_and_no_bump(world):
    vm = world.vm()
    row = fw.set_firewall(world.db, vm.id, world.actor("alice"), revision=1, policy_in="DROP")
    assert row.revision == 1 and _actions(world, vm.id) == []


# Eligibility


def _config(net0="virtio=BC:24:11:00:00:01,bridge=vmbr0", **nets):
    return {"net0": net0, **nets}


def test_eligible_vm(world):
    vm = world.vm()
    result = fw.eligibility(world.db, vm.id, vm_config=_config())
    assert result.eligible and result.reasons == [] and result.nics_checked


@pytest.mark.parametrize("config, reason", [
    (_config(net1="virtio=BC:24:11:00:00:02,bridge=vmbr230"), "infrastructure_bridge"),
    (_config(net1="virtio=BC:24:11:00:00:02,bridge=vmbr1,trunks=10;20"), "nic_trunk"),
    (_config(net1="virtio=02:52:46:00:00:02,bridge=vmbr9"), "storage_mac"),
    ({"net1": "virtio=BC:24:11:00:00:02,bridge=vmbr0"}, "no_net0"),
])
def test_ineligible_nics(world, config, reason):
    set_setting(world.db, "vm_firewall_infrastructure_bridges", "vmbr230")
    vm = world.vm()
    assert reason in fw.eligibility(world.db, vm.id, vm_config=config).reasons


def test_excluded_and_location_disabled(world):
    vm = world.vm()
    fw.set_admin_fields(world.db, vm.id, world.staff_actor, revision=1, excluded=True)
    assert fw.eligibility(world.db, vm.id).reasons == ["excluded"]
    world.db.get(FirewallLocationSettings, world.location.id).enabled = False
    world.db.flush()
    assert fw.eligibility(world.db, vm.id).reasons == ["location_disabled", "excluded"]


def test_ipv6_ra_bridge_needs_a_location_prefix(world):
    set_setting(world.db, "vm_firewall_ipv6_ra_bridges", "vmbr750")
    vm = world.vm()
    config = _config("virtio=BC:24:11:00:00:01,bridge=vmbr750")
    assert fw.eligibility(world.db, vm.id, vm_config=config).reasons == ["ipv6_ra_bridge_needs_prefix"]
    assert fw.eligibility(world.db, vm.id, vm_config=_config()).eligible
    fw.update_location_settings(world.db, world.staff_actor, world.location.id,
                                ipv6_allowed_prefixes=["2001:db8:750::/48"])
    assert fw.eligibility(world.db, vm.id, vm_config=config).eligible


def test_enabling_an_ineligible_vm_is_refused(world):
    set_setting(world.db, "vm_firewall_infrastructure_bridges", "vmbr230")
    vm = world.vm()
    with pytest.raises(FirewallNotEligible) as info:
        fw.set_firewall(world.db, vm.id, world.actor("alice"), revision=1, client_enabled=True,
                        vm_config=_config("virtio=BC:24:11:00:00:01,bridge=vmbr230"))
    assert info.value.reasons == ["infrastructure_bridge"]


def test_non_vm_services_have_no_firewall(world):
    vm = world.vm()
    vm.service_type = ServiceType.HTTP_PROXY
    world.db.flush()
    with pytest.raises(FirewallNotEligible):
        fw.get_or_create(world.db, vm.id)


# Permission key


def test_vm_firewall_permission():
    assert PermissionKey.VM_FIREWALL == "vm.firewall"
    entry = next(e for e in PERMISSION_CATALOG if e["key"] == PermissionKey.VM_FIREWALL)
    assert entry["label"] == "Manage the VM firewall" and entry["service_types"] == ["vm"]
    assert DEFAULT_PERMISSIONS_BY_SERVICE_TYPE[ServiceType.VM][PermissionKey.VM_FIREWALL] is True
