"""Validation of firewall rules, IP sets, baseline settings and references (section 4.6)."""

from types import SimpleNamespace

import pytest

from app.services.vm_firewall import validation as v
from app.services.vm_firewall.errors import FirewallValidationError


def _err(fn, *args, **kw) -> FirewallValidationError:
    with pytest.raises(FirewallValidationError) as info:
        fn(*args, **kw)
    return info.value


def rule(**kw):
    return {"direction": "in", "action": "ACCEPT", **kw}


def test_minimal_rule_normalises():
    spec = v.validate_rule(rule(protocol="TCP", ports=" 22 ", action="accept"))
    assert (spec.protocol, spec.ports, spec.action, spec.enabled) == ("tcp", "22", "ACCEPT", True)


@pytest.mark.parametrize(
    "ports, code",
    [
        ("0", "ports_invalid"),
        ("65536", "ports_invalid"),
        ("2000:1000", "ports_invalid"),
        ("22,22", "ports_invalid"),
        ("ssh", "ports_invalid"),
        (",".join(str(p) for p in range(1, 12)), "ports_too_many"),
        ("1:2,3:4,5:6,7:8,9:10,11:12,13:14,15:16", "ports_too_many"),
    ],
)
def test_bad_ports(ports, code):
    err = _err(v.validate_rule, rule(protocol="tcp", ports=ports), "rules[3]")
    assert (err.code, err.field) == (code, "rules[3].ports")


def test_ports_need_a_port_protocol():
    assert _err(v.validate_rule, rule(protocol="icmp", ports="22")).code == "ports_without_protocol"
    assert _err(v.validate_rule, rule(ports="22")).code == "ports_without_protocol"


def test_ten_items_and_ranges_allowed():
    assert v.parse_ports("1,2,3,4,5,6,7,8,9,10", "p") == "1,2,3,4,5,6,7,8,9,10"
    assert v.parse_ports("1000:2000,22", "p") == "1000:2000,22"


def test_cidr_host_bits_and_canonical_form():
    err = _err(v.validate_rule, rule(remote_cidrs=["10.0.0.1/24"]), "r")
    assert (err.code, err.field) == ("cidr_host_bits", "r.remote_cidrs[0]")
    spec = v.validate_rule(rule(remote_cidrs=["10.0.0.0/24", "192.0.2.5/32"]))
    assert spec.remote_cidrs == ["10.0.0.0/24", "192.0.2.5"]


def test_family_consistency():
    assert _err(v.validate_rule, rule(remote_cidrs=["10.0.0.0/8", "2001:db8::/32"])).code == "family_mismatch"
    assert _err(v.validate_rule, rule(protocol="icmp", remote_cidrs=["2001:db8::/32"])).code == "family_mismatch"
    assert _err(v.validate_rule, rule(ip_version="6", local_cidrs=["192.0.2.0/24"])).code == "family_mismatch"
    assert v.validate_rule(rule(protocol="ipv6-icmp", ip_version="6")).ip_version == "6"


def test_icmp_types():
    assert v.validate_rule(rule(protocol="icmp", icmp_type="echo-request")).icmp_type == "echo-request"
    assert _err(v.validate_rule, rule(protocol="icmp", icmp_type="pong")).code == "icmp_type_invalid"
    assert _err(v.validate_rule, rule(protocol="tcp", icmp_type="echo-request")).code == "icmp_type_without_icmp"
    assert _err(v.validate_rule, rule(protocol="icmp", icmp_type="neighbour-solicitation")).code == "icmp_type_invalid"


def test_at_most_twenty_cidrs():
    cidrs = [f"10.0.{i}.0/24" for i in range(21)]
    assert _err(v.validate_rule, rule(remote_cidrs=cidrs)).code == "cidr_too_many"


def test_addresses_and_ipset_are_exclusive():
    assert _err(v.validate_rule, rule(remote_cidrs=["10.0.0.0/8"], remote_ipset_id=3)).code == "remote_conflict"


def test_unknown_field_and_missing_direction():
    err = _err(v.validate_rule, {"direction": "in", "action": "ACCEPT", "macro": "SSH"}, "rule")
    assert (err.code, err.field) == ("unknown_field", "rule.macro")
    err = _err(v.validate_rule, {"action": "ACCEPT"}, "rule")
    assert (err.code, err.field) == ("required", "rule.direction")


def test_logging_needs_permission():
    assert _err(v.validate_rule, rule(log=True), allow_logging=False).code == "logging_not_allowed"
    assert v.validate_rule(rule(log=True)).log is True


def test_group_reference_rules():
    spec = v.validate_rule({"kind": "group", "group_id": 4})
    assert (spec.kind, spec.ref_group_id) == ("group", 4)
    assert _err(v.validate_rule, {"kind": "group", "group_id": 4, "ports": "22"}).code == "invalid_value"
    assert _err(v.validate_rule, {"kind": "group", "group_id": 4}, allow_group=False).code == "group_nesting"


def test_rule_list_limits():
    rules = [rule() for _ in range(3)]
    assert _err(v.validate_rules, rules, max_count=2).code == "limit_exceeded"
    refs = [{"kind": "group", "group_id": i} for i in (1, 2, 3)]
    assert _err(v.validate_rules, refs, max_count=10, max_group_refs=2).code == "limit_exceeded"
    err = _err(v.validate_rules, [{"kind": "group", "group_id": 1}] * 2, max_count=10)
    assert (err.code, err.field) == ("group_already_attached", "rules[1].group_id")


def test_ipset_entries():
    entries = v.validate_ipset_entries(["192.0.2.0/24", {"cidr": "2001:db8::/48", "nomatch": True}], max_entries=5)
    assert [(e.cidr, e.nomatch) for e in entries] == [("192.0.2.0/24", False), ("2001:db8::/48", True)]
    for bad, code in (("0.0.0.0/0", "any_network_not_allowed"), ("::/0", "any_network_not_allowed"),
                      ("10.0.0.1/8", "cidr_host_bits")):
        err = _err(v.validate_ipset_entries, [bad], max_entries=5)
        assert (err.code, err.field) == (code, "entries[0].cidr")
    assert _err(v.validate_ipset_entries, ["10.0.0.0/8", "10.0.0.0/8"], max_entries=5).code == "duplicate_entry"
    assert _err(v.validate_ipset_entries, ["10.0.0.0/8"] * 3, max_entries=2).code == "limit_exceeded"


def test_baseline_settings_smtp_out():
    assert v.validate_baseline_settings({"smtp_out": "block"}) == {"smtp_out": "block"}
    assert v.validate_baseline_settings({"smtp_out": None}) == {}
    assert _err(v.validate_baseline_settings, {"smtp_out": "deny"}).field == "settings.smtp_out"
    assert _err(v.validate_baseline_settings, {"something": 1}).code == "unknown_field"


def test_admin_overrides():
    out = v.validate_admin_overrides({"macfilter": False, "extra_ipfilter": ["203.0.113.9/32"]})
    assert out == {"macfilter": False, "extra_ipfilter": ["203.0.113.9"]}


def test_reference_rule_compares_with_the_service_owner():
    own = SimpleNamespace(scope="client", owner_user_id=7, client_selectable=False)
    other = SimpleNamespace(scope="client", owner_user_id=8, client_selectable=False)
    offered = SimpleNamespace(scope="admin", owner_user_id=None, client_selectable=True)
    private_admin = SimpleNamespace(scope="admin", owner_user_id=None, client_selectable=False)
    v.check_reference(own, 7, actor_is_staff=False, field_path="x")
    v.check_reference(offered, 7, actor_is_staff=False, field_path="x")
    v.check_reference(private_admin, 7, actor_is_staff=True, field_path="x")
    for obj, staff in ((other, False), (other, True), (private_admin, False)):
        err = _err(v.check_reference, obj, 7, actor_is_staff=staff, field_path="rules[0].group_id")
        assert (err.code, err.field) == ("reference_not_allowed", "rules[0].group_id")


def test_ipv6_prefixes():
    assert v.validate_prefixes(["2001:db8:1::/48"]) == ["2001:db8:1::/48"]
    assert v.validate_prefixes([]) is None
    assert _err(v.validate_prefixes, ["192.0.2.0/24"]).code == "family_mismatch"
