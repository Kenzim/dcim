"""The planner (section 5.1): positions valid at each step, guard placement, ordering of IP sets and enable."""

import random

import pytest

from app.services.vm_firewall.planner import (
    OP_IPSET_ADD,
    OP_IPSET_CREATE,
    OP_IPSET_DELETE,
    OP_IPSET_REMOVE,
    OP_OPTIONS,
    OP_RULE_CREATE,
    OP_RULE_DELETE,
    OP_RULE_MOVE,
    OP_RULE_UPDATE,
    RULE_OPS,
    CurrentFirewall,
    PlanError,
    Op,
    matches,
    plan,
    simulate,
)
from app.services.vm_firewall.render import GUARD_MARKER, IpsetEntry, RenderedFirewall, RenderedRule

OPTIONS = {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT", "ipfilter": 1, "macfilter": 1,
           "dhcp": 1, "ndp": 1, "radv": 0, "log_level_in": "nolog", "log_level_out": "nolog"}


def rr(marker, port, action="ACCEPT", direction="in", enable=1, **kw):
    return RenderedRule(type=direction, action=action, comment=f"rf:{marker}", proto="tcp", dport=str(port),
                        enable=enable, **kw)


def desired(rules, *, baseline=0, ipsets=None, **options):
    return RenderedFirewall(
        service_id=1,
        options={**OPTIONS, **options},
        ipsets=ipsets if ipsets is not None else {"ipfilter-net0": [IpsetEntry("203.0.113.5")]},
        rules=list(rules),
        baseline_rule_count=baseline,
        enforced=True,
        active=True,
    )


def current_of(d: RenderedFirewall, **options) -> CurrentFirewall:
    return CurrentFirewall(
        options={**d.options, **options},
        rules=[r.params() for r in d.rules],
        ipsets={k: list(v) for k, v in d.ipsets.items()},
    )


def run(current, ops):
    states = [current]
    for op in ops:
        states.append(simulate(states[-1], op))
    return states


def test_in_sync_plans_nothing():
    d = desired([rr("c1", 22)])
    assert plan(current_of(d), d) == []


def test_single_change_is_direct_and_inert_until_placed():
    old = desired([rr("b1", 1, "DROP"), rr("c1", 22)], baseline=1)
    new = desired([rr("b1", 1, "DROP"), rr("c1", 22), rr("c2", 80)], baseline=1)
    ops = plan(current_of(old), new)
    assert [op.kind for op in ops] == [OP_RULE_CREATE, OP_RULE_MOVE, OP_RULE_UPDATE]
    assert ops[0].params["enable"] == 0 and ops[2].params == {"enable": 1}
    assert all(GUARD_MARKER not in str(op.params) for op in ops)
    states = run(current_of(old), ops)
    # While the new ACCEPT sits above the baseline DROP it is disabled.
    assert states[1].rules[0]["enable"] == 0
    assert matches(states[-1], new)


def test_single_update_in_place():
    old = desired([rr("c1", 22)])
    new = desired([rr("c1", 2222)])
    ops = plan(current_of(old), new)
    assert [(op.kind, op.pos, op.params) for op in ops] == [(OP_RULE_UPDATE, 0, {"dport": "2222"})]


def test_several_changes_are_guarded_after_the_baseline():
    base = [rr("b1", 1, "DROP"), rr("b2", 2, "DROP")]
    old = desired(base + [rr("c1", 22)], baseline=2)
    new = desired(base + [rr("c2", 80), rr("c3", 443)], baseline=2)
    ops = plan(current_of(old), new)
    states = run(current_of(old), ops)
    guard_index = None
    for state in states[1:-1]:
        enabled = [i for i, r in enumerate(state.rules)
                   if r.get("comment") == GUARD_MARKER and str(r.get("enable")) == "1"]
        if enabled:
            guard_index = enabled[0]
            break
    assert guard_index == 2
    # The guard is enabled before any client rule changes, and removed by the last rule op.
    first_client_op = next(i for i, op in enumerate(ops) if op.kind == OP_RULE_DELETE or
                           (op.kind == OP_RULE_CREATE and op.params.get("comment") != GUARD_MARKER))
    guard_state = states[first_client_op]
    assert any(r.get("comment") == GUARD_MARKER and str(r.get("enable")) == "1" for r in guard_state.rules)
    rule_ops = [op for op in ops if op.kind in RULE_OPS]
    assert rule_ops[-1].kind == OP_RULE_DELETE and rule_ops[-1].pos == 2
    assert GUARD_MARKER not in [r.get("comment") for r in states[-1].rules]
    assert matches(states[-1], new)


def test_guard_adds_out_drop_only_when_outbound_changes():
    old = desired([rr("c1", 22)])
    new_in = desired([rr("c2", 80), rr("c3", 443)])
    ops = plan(current_of(old), new_in)
    guards = [op for op in ops if op.kind == OP_RULE_CREATE and op.params.get("comment") == GUARD_MARKER]
    assert [g.params["type"] for g in guards] == ["in"]
    new_out = desired([rr("c2", 80), rr("c3", 25, "DROP", "out")])
    guards = [op for op in plan(current_of(old), new_out)
              if op.kind == OP_RULE_CREATE and op.params.get("comment") == GUARD_MARKER]
    assert sorted(g.params["type"] for g in guards) == ["in", "out"]


def test_no_guard_when_the_firewall_is_off():
    old = desired([rr("c1", 22)], enable=0)
    new = desired([rr("c2", 80), rr("c3", 443)], enable=0)
    ops = plan(current_of(old), new)
    assert not any(op.params.get("comment") == GUARD_MARKER for op in ops)


def test_ipsets_written_before_rules_and_deleted_after():
    old = desired([rr("c1", 22, source="+rf-s9")],
                  ipsets={"ipfilter-net0": [IpsetEntry("203.0.113.5")], "rf-s9": [IpsetEntry("192.0.2.0/24")]})
    new = desired([rr("c2", 22, source="+rf-s4")],
                  ipsets={"ipfilter-net0": [IpsetEntry("203.0.113.5")], "rf-s4": [IpsetEntry("198.51.100.0/24")]})
    ops = plan(current_of(old), new)
    kinds = [op.kind for op in ops]
    first_rule = min(i for i, k in enumerate(kinds) if k in RULE_OPS)
    last_rule = max(i for i, k in enumerate(kinds) if k in RULE_OPS)
    assert kinds.index(OP_IPSET_CREATE) < first_rule and kinds.index(OP_IPSET_ADD) < first_rule
    assert kinds.index(OP_IPSET_DELETE) > last_rule
    assert matches(run(current_of(old), ops)[-1], new)


def test_entry_removed_after_rules():
    old = desired([rr("c1", 22)], ipsets={"ipfilter-net0": [IpsetEntry("203.0.113.5"), IpsetEntry("203.0.113.6")]})
    new = desired([rr("c1", 22), rr("c2", 80)], ipsets={"ipfilter-net0": [IpsetEntry("203.0.113.7")]})
    kinds = [op.kind for op in plan(current_of(old), new)]
    assert kinds.index(OP_IPSET_ADD) < kinds.index(OP_RULE_CREATE)
    assert kinds.index(OP_IPSET_REMOVE) > max(i for i, k in enumerate(kinds) if k in RULE_OPS)


def test_enable_last_when_turning_on_first_when_turning_off():
    off = desired([], enable=0)
    on = desired([rr("c1", 22), rr("c2", 80)], enable=1)
    ops = plan(current_of(off), on)
    assert ops[-1].kind == OP_OPTIONS and ops[-1].params == {"enable": 1}
    assert all("enable" not in op.params for op in ops[:-1] if op.kind == OP_OPTIONS)
    ops = plan(current_of(on), desired([], enable=0, policy_in="ACCEPT"))
    assert ops[0].kind == OP_OPTIONS and ops[0].params == {"enable": 0}


def test_unmarked_rules_are_removed():
    d = desired([rr("c1", 22)])
    current = current_of(d)
    current.rules.insert(0, {"type": "in", "action": "ACCEPT", "enable": 1, "comment": "added in the Proxmox UI"})
    current.rules.append({"type": "in", "action": "ACCEPT", "macro": "SSH", "enable": 1})
    ops = plan(current, d)
    assert [op.kind for op in ops].count(OP_RULE_DELETE) >= 2
    assert matches(run(current, ops)[-1], d)


def test_duplicate_markers_are_removed():
    d = desired([rr("c1", 22)])
    current = current_of(d)
    current.rules.append(dict(current.rules[0]))
    final = run(current, plan(current, d))[-1]
    assert [r["comment"] for r in final.rules] == ["rf:c1"]


def test_unknown_options_are_deleted():
    d = desired([])
    current = current_of(d)
    current.options["log_ratelimit"] = "enable=1"
    ops = plan(current, d)
    assert [op.delete for op in ops if op.kind == OP_OPTIONS] == [("log_ratelimit",)]


def test_simulate_rejects_invalid_positions():
    with pytest.raises(PlanError):
        simulate(CurrentFirewall(), Op(OP_RULE_DELETE, pos=0))


def _accepts(state, packet_port):
    """Whether an inbound tcp packet to ``packet_port`` is accepted (first match, policy DROP)."""
    for rule in state.rules:
        if str(rule.get("enable", 0)) != "1" or rule.get("type") != "in":
            continue
        port = rule.get("dport")
        if port is None or str(port) == str(packet_port):
            return rule["action"] == "ACCEPT"
    return str(state.options.get("policy_in", "DROP")) == "ACCEPT"


@pytest.mark.parametrize("seed", range(60))
def test_random_states_converge_with_valid_positions_and_no_window(seed):
    rnd = random.Random(seed)
    baseline = [rr(f"b{i}", 1000 + i, "DROP") for i in range(rnd.randint(0, 3))]

    def client_rules():
        markers = rnd.sample(range(1, 12), rnd.randint(0, 6))
        return [rr(f"c{m}", rnd.choice([22, 80, 443, 1000, 1001, 3306]), rnd.choice(["ACCEPT", "DROP"]),
                   enable=rnd.choice([1, 1, 0])) for m in markers]

    old = desired(baseline + client_rules(), baseline=len(baseline))
    new = desired(baseline + client_rules(), baseline=len(baseline))
    current = current_of(old)
    if rnd.random() < 0.3:
        current.rules.insert(rnd.randint(0, len(current.rules)),
                             {"type": "in", "action": "ACCEPT", "enable": 1, "comment": "foreign"})
    ops = plan(current, new)
    states = run(current, ops)  # raises PlanError if a position is invalid when it runs
    assert matches(states[-1], new)
    for port in (22, 80, 443, 1000, 1001, 1002, 3306, 9999):
        allowed = _accepts(states[0], port) or _accepts(states[-1], port)
        for state in states[1:-1]:
            assert allowed or not _accepts(state, port), f"port {port} opened mid-apply"
