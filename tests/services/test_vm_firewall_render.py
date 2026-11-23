"""Rendering of the Proxmox firewall document (section 4.5) and presets."""

import pytest

from app.models.vm_firewall import (
    FirewallBaseline,
    FirewallGroup,
    FirewallIpset,
    FirewallIpsetEntry,
    FirewallLocationSettings,
    FirewallRule,
)
from app.services.settings_store import set_setting
from app.services.vm_firewall import presets
from app.services.vm_firewall import service as fw
from app.services.vm_firewall.render import render
from tests.fakes.vm_firewall_world import make_world


@pytest.fixture
def world(db_session):
    return make_world(db_session)


def _on(world, service, **kw):
    row = fw.get_or_create(world.db, service.id)
    row.client_enabled = True
    for key, value in kw.items():
        setattr(row, key, value)
    world.db.flush()
    return row


def _baseline(world, scope_type="global", scope_id=None, settings=None, rules=()):
    return fw.create_baseline(
        world.db, world.staff_actor, name=f"{scope_type} baseline", scope_type=scope_type, scope_id=scope_id,
        settings=settings, rules=list(rules),
    )


def _comments(rendered):
    return [rule.comment for rule in rendered.rules]


def test_client_rules_follow_the_baseline(world):
    vm = world.vm()
    _baseline(world, rules=[{"direction": "in", "action": "DROP", "protocol": "tcp", "ports": "3306"}])
    row = _on(world, vm)
    fw.replace_rules(world.db, vm.id, world.actor("alice"),
                     [{"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "3306", "comment": "db"}],
                     revision=row.revision)
    rendered = render(world.db, vm.id)
    actions = [(r.action, r.comment.split(".")[0][:4]) for r in rendered.rules]
    assert actions[0][0] == "DROP" and rendered.rules[0].comment.startswith("rf:b")
    assert rendered.rules[-1].action == "ACCEPT" and rendered.rules[-1].comment.startswith("rf:c")
    assert rendered.baseline_rule_count == 1


def test_merge_order_global_location_product(world):
    vm = world.vm(product_code="vps-s")
    product = world.product("vps-s")
    _baseline(world, "product", product.id, rules=[{"direction": "in", "action": "DROP", "protocol": "tcp", "ports": "3"}])
    _baseline(world, "location", world.location.id, rules=[{"direction": "in", "action": "DROP", "protocol": "tcp", "ports": "2"}])
    _baseline(world, rules=[{"direction": "in", "action": "DROP", "protocol": "tcp", "ports": "1"}])
    _on(world, vm)
    assert [r.dport for r in render(world.db, vm.id).rules] == ["1", "2", "3"]


def test_product_setting_overrides_global(world):
    vm = world.vm(product_code="mail-ok")
    _baseline(world, settings={"smtp_out": "block"})
    _baseline(world, "product", world.product("mail-ok").id, settings={"smtp_out": "allow"})
    assert "rf:smtp25" not in _comments(render(world.db, vm.id))
    other = world.vm(product_code="plain")
    assert "rf:smtp25" in _comments(render(world.db, other.id))


def test_smtp_block_from_settings_and_admin_override(world):
    vm = world.vm()
    _on(world, vm)
    assert "rf:smtp25" not in _comments(render(world.db, vm.id))
    _baseline(world, settings={"smtp_out": "block"})
    smtp = [r for r in render(world.db, vm.id).rules if r.comment == "rf:smtp25"]
    assert [(r.type, r.action, r.proto, r.dport) for r in smtp] == [("out", "DROP", "tcp", "25")]
    row = fw.get_or_create(world.db, vm.id)
    fw.set_admin_fields(world.db, vm.id, world.staff_actor, revision=row.revision, admin_overrides={"smtp_out": "allow"})
    assert "rf:smtp25" not in _comments(render(world.db, vm.id))


def test_groups_expand_in_place(world):
    vm = world.vm()
    alice = world.actor("alice")
    group = fw.create_group(world.db, alice, name="web", rules=[
        {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"},
        {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "443"},
    ])
    row = _on(world, vm)
    fw.replace_rules(world.db, vm.id, alice, [
        {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22"},
        {"kind": "group", "group_id": group.id},
        {"direction": "in", "action": "DROP", "protocol": "tcp", "ports": "8080"},
    ], revision=row.revision)
    rendered = render(world.db, vm.id)
    assert [r.dport for r in rendered.rules] == ["22", "80", "443", "8080"]
    assert rendered.rules[1].comment.startswith(f"rf:g{group.id}.")


def test_disabled_rules_and_disabled_group_render_with_enable_0(world):
    vm = world.vm()
    alice = world.actor("alice")
    group = fw.create_group(world.db, alice, name="g", rules=[{"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}])
    row = _on(world, vm)
    fw.replace_rules(world.db, vm.id, alice, [
        {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22", "enabled": False},
        {"kind": "group", "group_id": group.id, "enabled": False},
    ], revision=row.revision)
    assert [r.enable for r in render(world.db, vm.id).rules] == [0, 0]


def test_client_off_keeps_baseline_and_accepts(world):
    vm = world.vm()
    _baseline(world, rules=[{"direction": "in", "action": "DROP", "protocol": "tcp", "ports": "3306"}])
    row = _on(world, vm, policy_in="DROP")
    fw.replace_rules(world.db, vm.id, world.actor("alice"),
                     [{"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22"}], revision=row.revision)
    row.client_enabled = False
    world.db.flush()
    rendered = render(world.db, vm.id)
    assert (rendered.options["policy_in"], rendered.options["policy_out"]) == ("ACCEPT", "ACCEPT")
    assert [r.comment[:4] for r in rendered.rules] == ["rf:b"]
    assert rendered.options["enable"] == 0
    world.db.get(FirewallLocationSettings, world.location.id).enforce_baseline_on_all = True
    world.db.flush()
    assert render(world.db, vm.id).options["enable"] == 1


def test_enable_follows_client_and_location(world):
    vm = world.vm()
    row = _on(world, vm)
    assert render(world.db, vm.id).options["enable"] == 1
    row.excluded = True
    world.db.flush()
    assert render(world.db, vm.id).options["enable"] == 0
    row.excluded = False
    world.db.get(FirewallLocationSettings, world.location.id).enabled = False
    world.db.flush()
    assert render(world.db, vm.id).options["enable"] == 0


def test_options_defaults(world):
    vm = world.vm()
    _on(world, vm)
    options = render(world.db, vm.id).options
    assert options == {
        "enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT", "ipfilter": 1, "macfilter": 1,
        "dhcp": 1, "ndp": 1, "radv": 0, "log_level_in": "nolog", "log_level_out": "nolog",
    }


def test_no_address_means_no_ipfilter(world):
    vm = world.vm(ip=None)
    _on(world, vm)
    rendered = render(world.db, vm.id)
    assert rendered.ipsets["ipfilter-net0"] == [] and rendered.options["ipfilter"] == 0


def test_ipfilter_has_address_overrides_and_location_prefixes(world):
    set_setting(world.db, "vm_firewall_ipv6_ra_bridges", "vmbr750")
    vm = world.vm(ip="203.0.113.20")
    row = _on(world, vm, net0_bridge="vmbr750")
    fw.set_admin_fields(world.db, vm.id, world.staff_actor, revision=row.revision,
                        admin_overrides={"extra_ipfilter": ["203.0.113.21"]})
    world.db.get(FirewallLocationSettings, world.location.id).ipv6_allowed_prefixes = ["2001:db8:7::/48"]
    world.db.flush()
    cidrs = [e.cidr for e in render(world.db, vm.id).ipsets["ipfilter-net0"]]
    assert cidrs == ["2001:db8:7::/48", "203.0.113.20", "203.0.113.21"]


def _net0(bridge):
    return {"net0": f"virtio=BC:24:11:00:00:01,bridge={bridge}"}


def test_ipv6_prefixes_only_for_vms_observed_on_an_ra_bridge(world):
    set_setting(world.db, "vm_firewall_ipv6_ra_bridges", "vmbr750")
    world.db.get(FirewallLocationSettings, world.location.id).ipv6_allowed_prefixes = ["2001:db8:750::/48"]
    world.db.flush()
    ra_vm, plain_vm = world.vm(ip="203.0.113.30"), world.vm(ip="203.0.113.31")
    for vm, bridge in ((ra_vm, "vmbr750"), (plain_vm, "vmbr0")):
        row = _on(world, vm)
        assert fw.eligibility(world.db, vm.id, vm_config=_net0(bridge)).eligible
        assert (row.net0_bridge, row.revision) == (bridge, 2)
    ra, plain = render(world.db, ra_vm.id), render(world.db, plain_vm.id)
    assert [e.cidr for e in ra.ipsets["ipfilter-net0"]] == ["2001:db8:750::/48", "203.0.113.30"]
    assert [e.cidr for e in plain.ipsets["ipfilter-net0"]] == ["203.0.113.31"]
    assert ra.options["ipfilter"] == plain.options["ipfilter"] == 1
    assert ra.reasons == plain.reasons == []


def test_unknown_net0_bridge_fails_safe_while_ra_bridges_are_listed(world):
    world.db.get(FirewallLocationSettings, world.location.id).ipv6_allowed_prefixes = ["2001:db8:750::/48"]
    world.db.flush()
    vm = world.vm(ip="203.0.113.40")
    row = _on(world, vm)
    assert row.net0_bridge is None
    # No router-advert bridge configured: an unknown bridge does not matter.
    assert render(world.db, vm.id).options["ipfilter"] == 1
    set_setting(world.db, "vm_firewall_ipv6_ra_bridges", "vmbr750")
    rendered = render(world.db, vm.id)
    assert rendered.options["ipfilter"] == 0 and rendered.reasons == ["net0_bridge_unknown"]
    assert [e.cidr for e in rendered.ipsets["ipfilter-net0"]] == ["203.0.113.40"]


def test_markers_only_never_client_text(world):
    vm = world.vm()
    alice = world.actor("alice")
    ipset = fw.create_ipset(world.db, alice, name="office <script>", description="secret office",
                            entries=[{"cidr": "198.51.100.0/24", "comment": "Alice's house"}])
    row = _on(world, vm)
    fw.replace_rules(world.db, vm.id, alice, [
        {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22", "remote_ipset_id": ipset.id,
         "comment": "evil'; rm -rf"},
    ], revision=row.revision)
    rendered = render(world.db, vm.id)
    text = rendered.canonical()
    for secret in ("evil", "Alice", "office", "secret"):
        assert secret not in text
    assert all(rule.comment.startswith("rf:") for rule in rendered.rules)
    assert rendered.rules[-1].source == f"+rf-s{ipset.id}"
    assert [e.cidr for e in rendered.ipsets[f"rf-s{ipset.id}"]] == ["198.51.100.0/24"]


def test_management_sets_block_both_ways(world):
    vm = world.vm()
    mgmt = fw.create_ipset(world.db, world.staff_actor, name="mgmt", purpose="management",
                           entries=["10.255.0.0/16"])
    _on(world, vm)
    rendered = render(world.db, vm.id)
    pairs = [(r.type, r.action, r.source, r.dest) for r in rendered.rules]
    ref = f"+rf-s{mgmt.id}"
    assert ("out", "DROP", None, ref) in pairs and ("in", "DROP", ref, None) in pairs
    assert f"rf-s{mgmt.id}" in rendered.ipsets
    _baseline(world, settings={"block_management": False})
    assert render(world.db, vm.id).rules == []


def test_ip_version_without_addresses(world):
    vm = world.vm()
    row = _on(world, vm)
    fw.replace_rules(world.db, vm.id, world.actor("alice"),
                     [{"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22", "ip_version": "6"}],
                     revision=row.revision)
    assert render(world.db, vm.id).rules[0].source == "::/0"


def test_hash_is_canonical_and_changes(world):
    vm = world.vm()
    _on(world, vm)
    first = render(world.db, vm.id)
    assert render(world.db, vm.id).desired_hash == first.desired_hash
    _baseline(world, settings={"smtp_out": "block"})
    assert render(world.db, vm.id).desired_hash != first.desired_hash


def test_render_is_read_only(world):
    vm = world.vm()
    _on(world, vm)
    world.db.commit()
    render(world.db, vm.id)
    assert not world.db.new and not world.db.dirty


def test_unrelated_objects_do_not_render(world):
    vm = world.vm()
    _on(world, vm)
    world.db.add_all([
        FirewallIpset(name="x", scope="client", owner_user_id=world.user("bob").id, client_selectable=False,
                      purpose="general", revision=1),
        FirewallGroup(name="y", scope="admin", client_selectable=True, revision=1),
        FirewallBaseline(name="z", scope_type="location", scope_id=world.location.id + 999, enabled=True, revision=1),
    ])
    world.db.flush()
    world.db.add(FirewallIpsetEntry(ipset_id=world.db.query(FirewallIpset).first().id, cidr="192.0.2.0/24"))
    world.db.add(FirewallRule(baseline_id=world.db.query(FirewallBaseline).first().id, position=0, kind="rule",
                              direction="in", action="DROP", ip_version="any"))
    world.db.flush()
    rendered = render(world.db, vm.id)
    assert rendered.rules == [] and list(rendered.ipsets) == ["ipfilter-net0"]


# Presets


def test_preset_definitions():
    web = presets.PRESETS["web"].rules
    assert [(r["protocol"], r["ports"]) for r in web] == [("tcp", "80,443"), ("udp", "443")]
    ping = presets.PRESETS["ping"].rules
    assert [(r["protocol"], r["icmp_type"]) for r in ping] == [("icmp", "echo-request"), ("ipv6-icmp", "echo-request")]
    assert presets.PRESETS["ssh"].rules[0]["ports"] == "22"
    assert presets.PRESETS["rdp"].rules[0]["ports"] == "3389"
    assert presets.PRESETS["allow_all"].rules == () and presets.PRESETS["allow_all"].policy_in == "ACCEPT"


@pytest.mark.parametrize("os_type, expected", [
    ("linux", ("ssh", "ping")), ("linux-cloudinit", ("ssh", "ping")), ("windows", ("rdp", "ping")),
    ("Unknown", ("ssh", "rdp", "ping")), (None, ("ssh", "rdp", "ping")),
])
def test_suggestions_from_template_os(world, os_type, expected):
    vm = world.vm(os_type=os_type)
    assert presets.suggested_presets(world.db, vm.id) == expected


def test_apply_preset_appends_once_and_allow_all_sets_policy(world):
    vm = world.vm()
    alice = world.actor("alice")
    row = _on(world, vm)
    fw.apply_preset(world.db, vm.id, alice, "web", revision=row.revision)
    fw.apply_preset(world.db, vm.id, alice, "web", revision=row.revision)
    assert [r.ports for r in fw.service_rules(world.db, vm.id)] == ["80,443", "443"]
    revision = row.revision
    fw.apply_preset(world.db, vm.id, alice, "allow_all", revision=revision)
    assert row.policy_in == "ACCEPT" and row.revision == revision + 1
    assert len(fw.service_rules(world.db, vm.id)) == 2


def test_settings_defaults(db_session):
    from app.services.settings_store import get_setting

    assert get_setting(db_session, "vm_firewall_max_rules_per_vm") == 100
    assert get_setting(db_session, "vm_firewall_drift_mode") == "repair"
    assert get_setting(db_session, "firewall_client_logging") is False
    assert get_setting(db_session, "vm_firewall_infrastructure_bridges") == ""
    set_setting(db_session, "vm_firewall_infrastructure_bridges", "vmbr230, vmbr231", actor_user_id=None)
    assert get_setting(db_session, "vm_firewall_infrastructure_bridges") == "vmbr230,vmbr231"
