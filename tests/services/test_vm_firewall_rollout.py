"""Staged rollout of a location's ``enforce_baseline_on_all`` (section 8.3): a preview, a
confirmation equal to the previewed count, and ``rollout_after`` times staggered per node."""

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app.models.commerce_audit import UserAuditEvent
from app.models.vm_firewall import FirewallLocationSettings, FirewallNicFlag, ServiceFirewall
from app.services.settings_store import set_setting
from app.services.vm_firewall import service as fw
from app.services.vm_firewall.errors import FirewallConfirmationRequired
from app.services.vm_firewall.render import as_utc, render
from tests.fakes.vm_firewall_world import make_world

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(db_session):
    return make_world(db_session)


def _location(world) -> FirewallLocationSettings:
    return world.db.get(FirewallLocationSettings, world.location.id)


def _opted_in(world):
    vm = world.vm()
    row = fw.get_or_create(world.db, vm.id)
    row.client_enabled = True
    world.db.flush()
    return vm


def _switch_on(world, **kw):
    return fw.update_location_settings(world.db, world.staff_actor, world.location.id,
                                       enforce_baseline_on_all=True, now=NOW, **kw)


def test_switching_on_without_confirmation_is_refused_with_a_preview(world):
    plain = [world.vm(), world.vm()]
    _opted_in(world)
    with pytest.raises(FirewallConfirmationRequired) as caught:
        _switch_on(world)
    error = caught.value
    assert (error.code, error.http_status, error.field) == ("confirmation_required", 409, "confirm_existing_vms")
    # The opted-in VM is not affected by the switch, so it is not counted.
    assert error.preview["changed"] == 2
    assert error.preview["service_ids"] == [vm.id for vm in plain]
    assert error.to_dict()["preview"] == error.preview
    assert _location(world).enforce_baseline_on_all is False


def test_a_stale_count_is_refused(world):
    world.vm()
    world.vm()
    assert fw.preview_enforce_baseline_on_all(world.db, world.location.id)["changed"] == 2
    world.vm()  # a VM provisioned after the admin looked
    with pytest.raises(FirewallConfirmationRequired) as caught:
        _switch_on(world, confirm_existing_vms=2)
    assert caught.value.preview["changed"] == 3
    assert _location(world).enforce_baseline_on_all is False


def test_the_right_count_is_accepted_staged_and_audited(world):
    plain = [world.vm(), world.vm()]
    opted = _opted_in(world)
    opted_revision = fw.get_or_create(world.db, opted.id).revision
    _switch_on(world, confirm_existing_vms=2)
    assert _location(world).enforce_baseline_on_all is True
    for vm in plain:
        row = world.db.get(ServiceFirewall, vm.id)
        assert as_utc(row.rollout_after) == NOW and row.revision == 1
    opted_row = world.db.get(ServiceFirewall, opted.id)
    assert opted_row.rollout_after is None and opted_row.revision == opted_revision
    event = (world.db.query(UserAuditEvent).filter(UserAuditEvent.action == "firewall.location_updated")
             .order_by(UserAuditEvent.id.desc()).first())
    rollout = event.event_metadata["rollout"]
    assert (rollout["changed"], rollout["blip"], rollout["nic_flag_unknown"]) == (2, 0, 2)
    assert rollout["last_due_at"] == NOW.isoformat()


def test_staggering_respects_the_per_node_limit(world):
    set_setting(world.db, "vm_firewall_rollout_per_node_per_minute", 2)
    vms = [world.vm(node="pve1") for _ in range(5)] + [world.vm(node="pve2") for _ in range(3)]
    _switch_on(world, confirm_existing_vms=8)
    due = {vm.id: as_utc(world.db.get(ServiceFirewall, vm.id).rollout_after) for vm in vms}
    per_slot = Counter((vm.vm.proxmox_node_name, due[vm.id]) for vm in vms)
    assert max(per_slot.values()) == 2
    minute = timedelta(minutes=1)
    assert [due[vm.id] - NOW for vm in vms[:5]] == [0 * minute, 0 * minute, minute, minute, 2 * minute]
    assert [due[vm.id] - NOW for vm in vms[5:]] == [0 * minute, 0 * minute, minute]


def test_render_ignores_the_switch_until_rollout_after(world):
    set_setting(world.db, "vm_firewall_rollout_per_node_per_minute", 1)
    first, second = world.vm(), world.vm()
    _switch_on(world, confirm_existing_vms=2)
    later = NOW + timedelta(minutes=1)
    assert render(world.db, first.id, now=NOW).options["enable"] == 1
    before = render(world.db, second.id, now=NOW)
    assert before.options["enable"] == 0
    after = render(world.db, second.id, now=later)
    assert (after.options["enable"], after.options["ipfilter"], after.options["macfilter"]) == (1, 1, 1)
    assert after.desired_hash != before.desired_hash

    assert fw.release_due_rollouts(world.db, now=NOW) == [first.id]
    row = world.db.get(ServiceFirewall, second.id)
    assert row.revision == 1 and row.rollout_after is not None
    assert fw.release_due_rollouts(world.db, now=later) == [second.id]
    assert (row.revision, row.rollout_after) == (2, None)
    assert render(world.db, second.id, now=NOW).options["enable"] == 1


def test_turning_off_needs_no_confirmation_and_cancels_the_rollout(world):
    set_setting(world.db, "vm_firewall_rollout_per_node_per_minute", 1)
    first, second = world.vm(), world.vm()
    _switch_on(world, confirm_existing_vms=2)
    fw.update_location_settings(world.db, world.staff_actor, world.location.id, enforce_baseline_on_all=False)
    assert _location(world).enforce_baseline_on_all is False
    for vm in (first, second):
        row = world.db.get(ServiceFirewall, vm.id)
        assert row.rollout_after is None and row.revision == 2
        assert render(world.db, vm.id).options["enable"] == 0


def test_enabling_a_location_that_has_the_switch_needs_confirmation(world):
    location = _location(world)
    location.enabled = False
    location.enforce_baseline_on_all = True
    world.db.flush()
    world.vm()
    with pytest.raises(FirewallConfirmationRequired) as caught:
        fw.update_location_settings(world.db, world.staff_actor, world.location.id, enabled=True, staging=True)
    assert caught.value.preview["changed"] == 1
    fw.update_location_settings(world.db, world.staff_actor, world.location.id, enabled=True, staging=True,
                                confirm_existing_vms=1, now=NOW)
    assert _location(world).enabled is True


def test_preview_blip_counts_from_observed_flags_and_from_vm_configs(world):
    flags = {}
    for flag in (FirewallNicFlag.OFF, FirewallNicFlag.ON, FirewallNicFlag.UNKNOWN):
        vm = world.vm()
        fw.get_or_create(world.db, vm.id).nic_flag = flag
        flags[flag] = vm.id
    world.db.flush()
    preview = fw.preview_enforce_baseline_on_all(world.db, world.location.id)
    assert preview["changed"] == 3
    assert preview["blip_service_ids"] == [flags[FirewallNicFlag.OFF]]
    assert preview["nic_flag_unknown_service_ids"] == [flags[FirewallNicFlag.UNKNOWN]]

    net0 = "virtio=BC:24:11:00:00:01,bridge=vmbr0"
    configs = {
        flags[FirewallNicFlag.OFF]: {"net0": net0 + ",firewall=1"},
        flags[FirewallNicFlag.ON]: {"net0": net0, "status": "running"},
        flags[FirewallNicFlag.UNKNOWN]: {"net0": net0, "status": "stopped"},
    }
    preview = fw.preview_enforce_baseline_on_all(world.db, world.location.id, vm_configs=configs)
    assert preview["blip_service_ids"] == [flags[FirewallNicFlag.ON]]
    assert preview["nic_flag_unknown"] == 0
    # The preview writes nothing: net0's bridge is not recorded from the configs.
    assert all(world.db.get(ServiceFirewall, sid).net0_bridge is None for sid in flags.values())


def test_preview_leaves_out_ineligible_vms(world):
    excluded = world.vm()
    fw.get_or_create(world.db, excluded.id).excluded = True
    trunked = world.vm()
    world.vm()
    configs = {trunked.id: {"net0": "virtio=BC:24:11:00:00:02,bridge=vmbr0,trunks=10;20"}}
    preview = fw.preview_enforce_baseline_on_all(world.db, world.location.id, vm_configs=configs)
    assert preview["changed"] == 1 and excluded.id not in preview["service_ids"]
    assert trunked.id not in preview["service_ids"]
