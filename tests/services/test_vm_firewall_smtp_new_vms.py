"""SMTP block on new VMs (section 4.5a): new VMs in enabled locations block outbound TCP 25 from
creation; existing VMs never inherit it; staff unblock per service; clients cannot change it."""

import pytest

from app.models.commerce_audit import UserAuditEvent
from app.models.vm_firewall import FirewallLocationSettings
from app.services.vm_firewall import service as fw
from app.services.vm_firewall.apply import apply
from app.services.vm_firewall.errors import FirewallForbidden
from app.services.vm_firewall.render import render
from app.services.vm_firewall.service import Actor
from tests.fakes.proxmox_firewall import FakeProxmoxFirewall
from tests.fakes.vm_firewall_world import make_world


@pytest.fixture
def world(db_session):
    return make_world(db_session)


def _smtp_rules(rendered):
    return [(r.type, r.action, r.proto, r.dport) for r in rendered.rules if r.dport in ("25", "465", "587")]


def _location(world) -> FirewallLocationSettings:
    return world.db.get(FirewallLocationSettings, world.location.id)


def test_location_defaults(world):
    location = _location(world)
    assert location.smtp_block_new_vms is True and location.anti_spoof_new_vms is False
    created = fw.get_location_settings(world.db, world.location.id + 1000)
    assert (created.smtp_block_new_vms, created.anti_spoof_new_vms) == (True, False)


def test_new_vm_in_an_enabled_location_blocks_port_25_only(world):
    vm = world.vm()
    row = fw.init_for_new_vm(world.db, vm.id, Actor.system())
    assert row.enforce_baseline is True and row.admin_overrides == {"smtp_out": "block"}
    assert row.client_enabled is False
    rendered = render(world.db, vm.id)
    assert rendered.options["enable"] == 1
    assert (rendered.options["policy_in"], rendered.options["policy_out"]) == ("ACCEPT", "ACCEPT")
    assert _smtp_rules(rendered) == [("out", "DROP", "tcp", "25")]
    # The narrow baseline: no anti-spoofing or MAC filter until the client turns the firewall on.
    assert (rendered.options["ipfilter"], rendered.options["macfilter"]) == (0, 0)
    assert [r.comment for r in rendered.rules] == ["rf:smtp25"]


def test_anti_spoof_new_vms_adds_ipfilter_and_macfilter(world):
    _location(world).anti_spoof_new_vms = True
    world.db.flush()
    vm = world.vm()
    fw.init_for_new_vm(world.db, vm.id, Actor.system())
    options = render(world.db, vm.id).options
    assert (options["ipfilter"], options["macfilter"], options["policy_in"]) == (1, 1, "ACCEPT")


def test_client_turning_on_keeps_the_block_and_gets_the_full_baseline(world):
    vm = world.vm()
    row = fw.init_for_new_vm(world.db, vm.id, Actor.system())
    fw.set_firewall(world.db, vm.id, world.actor("alice"), revision=row.revision, client_enabled=True)
    rendered = render(world.db, vm.id)
    assert _smtp_rules(rendered) == [("out", "DROP", "tcp", "25")]
    assert (rendered.options["policy_in"], rendered.options["ipfilter"], rendered.options["macfilter"]) == ("DROP", 1, 1)


def test_existing_vm_opting_in_renders_no_smtp_rule(world):
    vm = world.vm()
    row = fw.get_or_create(world.db, vm.id)
    fw.set_firewall(world.db, vm.id, world.actor("alice"), revision=row.revision, client_enabled=True)
    rendered = render(world.db, vm.id)
    assert rendered.options["enable"] == 1 and _smtp_rules(rendered) == []
    assert row.enforce_baseline is False


def test_baseline_smtp_out_defaults_to_allow(world):
    vm = world.vm(product_code="vps")
    fw.create_baseline(world.db, world.staff_actor, name="g", scope_type="global", settings={"smtp_out": None})
    fw.create_baseline(world.db, world.staff_actor, name="l", scope_type="location", scope_id=world.location.id)
    fw.create_baseline(world.db, world.staff_actor, name="p", scope_type="product", scope_id=world.product("vps").id)
    row = fw.get_or_create(world.db, vm.id)
    row.client_enabled = True
    world.db.flush()
    assert render(world.db, vm.id).settings["smtp_out"] is None
    assert _smtp_rules(render(world.db, vm.id)) == []


def test_staff_override_to_allow_removes_the_block(world):
    vm = world.vm()
    row = fw.init_for_new_vm(world.db, vm.id, Actor.system())
    revision = row.revision
    fw.set_admin_fields(world.db, vm.id, world.staff_actor, revision=revision, admin_overrides={"smtp_out": "allow"})
    assert row.revision == revision + 1
    rendered = render(world.db, vm.id)
    assert _smtp_rules(rendered) == []
    # Nothing else is enforced on the narrow baseline, so the firewall switches off.
    assert rendered.options["enable"] == 0
    actions = [e.action for e in world.db.query(UserAuditEvent)
               .filter(UserAuditEvent.resource_id == str(vm.id)).order_by(UserAuditEvent.id)]
    assert actions == ["firewall.admin_override_changed", "firewall.admin_override_changed"]


@pytest.mark.parametrize("kind", ["client", "billing"])
def test_non_staff_cannot_change_the_smtp_block(world, kind):
    vm = world.vm("alice")
    row = fw.init_for_new_vm(world.db, vm.id, Actor.system())
    alice = world.user("alice").id
    actor = Actor.client(alice) if kind == "client" else Actor.billing(1, alice)
    with pytest.raises(FirewallForbidden):
        fw.set_admin_fields(world.db, vm.id, actor, revision=row.revision, admin_overrides={"smtp_out": "allow"})
    with pytest.raises(FirewallForbidden):
        fw.set_admin_fields(world.db, vm.id, actor, revision=row.revision, enforce_baseline=False)
    with pytest.raises(FirewallForbidden):
        fw.init_for_new_vm(world.db, world.vm("alice").id, actor)
    assert row.admin_overrides == {"smtp_out": "block"} and row.enforce_baseline is True


def test_plain_row_outside_enabled_locations_or_when_switched_off(world):
    _location(world).smtp_block_new_vms = False
    world.db.flush()
    vm = world.vm()
    row = fw.init_for_new_vm(world.db, vm.id, Actor.system())
    assert (row.enforce_baseline, row.admin_overrides) == (False, None)
    assert render(world.db, vm.id).options["enable"] == 0

    other = make_world(world.db, location_enabled=False)
    vm2 = other.vm()
    row2 = fw.init_for_new_vm(world.db, vm2.id, Actor.system())
    assert (row2.enforce_baseline, row2.admin_overrides) == (False, None)


def test_init_is_idempotent_and_keeps_a_staff_unblock(world):
    vm = world.vm()
    row = fw.init_for_new_vm(world.db, vm.id, Actor.system())
    fw.set_admin_fields(world.db, vm.id, world.staff_actor, revision=row.revision, admin_overrides={"smtp_out": "allow"})
    again = fw.init_for_new_vm(world.db, vm.id, Actor.system())
    assert again is row and row.admin_overrides == {"smtp_out": "allow"}


def test_staff_can_clear_enforce_baseline(world):
    vm = world.vm()
    row = fw.init_for_new_vm(world.db, vm.id, Actor.system())
    fw.set_admin_fields(world.db, vm.id, world.staff_actor, revision=row.revision, enforce_baseline=False)
    assert render(world.db, vm.id).options["enable"] == 0


def test_toggling_smtp_block_new_vms_does_not_touch_existing_vms(world):
    vm = world.vm()
    row = fw.get_or_create(world.db, vm.id)
    fw.update_location_settings(world.db, world.staff_actor, world.location.id, smtp_block_new_vms=False)
    fw.update_location_settings(world.db, world.staff_actor, world.location.id, smtp_block_new_vms=True)
    assert row.revision == 1 and _smtp_rules(render(world.db, vm.id)) == []


@pytest.mark.asyncio
async def test_new_vm_applies_to_proxmox_with_the_block(world):
    vm = world.vm()
    fw.init_for_new_vm(world.db, vm.id, Actor.system())
    fake = FakeProxmoxFirewall()
    fake_vm = fake.add_vm(vm.vm.proxmox_vmid)
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.ok and result.state == "applied"
    assert fake_vm.options["enable"] == 1 and fake_vm.options["policy_in"] == "ACCEPT"
    assert [(r["type"], r["action"], r.get("dport"), r["enable"]) for r in fake_vm.rules] == [("out", "DROP", "25", 1)]
