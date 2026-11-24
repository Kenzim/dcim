"""Plan the Proxmox calls that turn a VM's current firewall into the rendered one (section 5.1).

``plan`` is pure. It simulates every operation with Proxmox's own semantics, so each position is
valid at the moment the operation runs:

- ``POST .../rules`` always inserts at position 0 (and a rule without ``enable`` is disabled);
- ``PUT .../rules/{pos}`` with ``moveto`` puts the rule before the rule that was at ``moveto``
  (or last when ``moveto`` is past the end);
- ``POST .../ipset/{name}`` inserts an entry; ``DELETE`` removes one.

New rules are therefore created disabled at the top (inert), moved into place and only then
enabled, so even a single unguarded insert never opens a permissive window. More than one change
to a live firewall is wrapped in a guard (``IN DROP``, plus ``OUT DROP`` when outbound rules
change, comment ``rf:guard``) right after the unchanged baseline, removed last. IP sets and their
entries are added before the rules and removed after. ``enable`` is written first when turning the
firewall off and last when turning it on. Rules without a known ``rf:`` marker are removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.services.vm_firewall.render import (
    GUARD_MARKER,
    MARKER_PREFIX,
    OPTION_KEYS,
    IpsetEntry,
    RenderedFirewall,
)

OP_OPTIONS = "options"
OP_IPSET_CREATE = "ipset_create"
OP_IPSET_DELETE = "ipset_delete"
OP_IPSET_ADD = "ipset_add"
OP_IPSET_UPDATE = "ipset_update"
OP_IPSET_REMOVE = "ipset_remove"
OP_RULE_CREATE = "rule_create"
OP_RULE_UPDATE = "rule_update"
OP_RULE_MOVE = "rule_move"
OP_RULE_DELETE = "rule_delete"
RULE_OPS = (OP_RULE_CREATE, OP_RULE_UPDATE, OP_RULE_MOVE, OP_RULE_DELETE)

# Rule properties Rackflow compares and may delete; anything else Proxmox returns (pos, digest,
# ipversion, errors) is derived and ignored.
RULE_PROPERTIES = (
    "type", "action", "proto", "dport", "sport", "source", "dest", "icmp-type", "enable", "log",
    "comment", "macro", "iface",
)
DELETABLE_RULE_PROPERTIES = tuple(k for k in RULE_PROPERTIES if k not in ("type", "action", "enable"))


class PlanError(ValueError):
    """An operation is not valid against the state it would run on."""


@dataclass
class CurrentFirewall:
    """What Proxmox holds for one VM. ``rules`` are rule dicts in position order."""

    options: dict[str, Any] = field(default_factory=dict)
    rules: list[dict[str, Any]] = field(default_factory=list)
    ipsets: dict[str, list[IpsetEntry]] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)

    def copy(self) -> "CurrentFirewall":
        return CurrentFirewall(
            options=dict(self.options),
            rules=[dict(rule) for rule in self.rules],
            ipsets={name: list(entries) for name, entries in self.ipsets.items()},
            digests=dict(self.digests),
        )


@dataclass
class Op:
    kind: str
    pos: Optional[int] = None
    moveto: Optional[int] = None
    params: dict[str, Any] = field(default_factory=dict)
    delete: tuple[str, ...] = ()
    ipset: Optional[str] = None
    cidr: Optional[str] = None
    nomatch: bool = False

    def describe(self) -> str:
        if self.kind in RULE_OPS:
            extra = f" moveto={self.moveto}" if self.moveto is not None else ""
            return f"{self.kind} pos={self.pos}{extra} {self.params.get('comment', '')}".strip()
        if self.kind == OP_OPTIONS:
            return f"options {sorted(self.params)} delete={list(self.delete)}"
        return f"{self.kind} {self.ipset} {self.cidr or ''}".strip()


def _str(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def normalise_rule(rule: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in RULE_PROPERTIES:
        value = rule.get(key)
        if value is None or value == "":
            continue
        if key == "log" and value == "nolog":
            continue
        out[key] = _str(value).lower() if key == "type" else _str(value)
    out.setdefault("enable", "0")
    return out


def normalise_options(options: dict[str, Any]) -> dict[str, str]:
    return {key: _str(value) for key, value in options.items() if key != "digest" and value is not None}


def marker(rule: dict[str, Any]) -> Optional[str]:
    comment = rule.get("comment")
    if isinstance(comment, str) and comment.startswith(MARKER_PREFIX):
        return comment
    return None


def simulate(state: CurrentFirewall, op: Op) -> CurrentFirewall:
    """The state after ``op``, with Proxmox's semantics; raises ``PlanError`` if it is invalid."""
    new = state.copy()
    rules = new.rules
    if op.kind == OP_OPTIONS:
        for key in op.delete:
            new.options.pop(key, None)
        new.options.update(op.params)
    elif op.kind == OP_RULE_CREATE:
        rule = {k: v for k, v in op.params.items() if k in RULE_PROPERTIES}
        rule.setdefault("enable", 0)
        rules.insert(0, rule)
    elif op.kind in (OP_RULE_UPDATE, OP_RULE_MOVE, OP_RULE_DELETE):
        if op.pos is None or not 0 <= op.pos < len(rules):
            raise PlanError(f"no rule at position {op.pos}")
        if op.kind == OP_RULE_DELETE:
            del rules[op.pos]
        elif op.kind == OP_RULE_MOVE:
            rule = rules[op.pos]
            moved = [r for i, r in enumerate(rules) if i != op.pos]
            # Proxmox puts the rule before the one that was at ``moveto``.
            target = op.moveto if op.moveto <= op.pos else op.moveto - 1
            moved.insert(min(target, len(moved)), rule)
            new.rules = moved
        else:
            rule = rules[op.pos]
            rule.update(op.params)
            for key in op.delete:
                rule.pop(key, None)
    elif op.kind == OP_IPSET_CREATE:
        if op.ipset in new.ipsets:
            raise PlanError(f"IP set {op.ipset} already exists")
        new.ipsets[op.ipset] = []
    elif op.kind == OP_IPSET_DELETE:
        if op.ipset not in new.ipsets:
            raise PlanError(f"no IP set {op.ipset}")
        del new.ipsets[op.ipset]
    elif op.kind in (OP_IPSET_ADD, OP_IPSET_UPDATE, OP_IPSET_REMOVE):
        entries = new.ipsets.get(op.ipset)
        if entries is None:
            raise PlanError(f"no IP set {op.ipset}")
        index = next((i for i, e in enumerate(entries) if e.cidr == op.cidr), None)
        if op.kind == OP_IPSET_ADD:
            if index is not None:
                raise PlanError(f"{op.cidr} already in {op.ipset}")
            entries.insert(0, IpsetEntry(op.cidr, op.nomatch))
        elif index is None:
            raise PlanError(f"{op.cidr} not in {op.ipset}")
        elif op.kind == OP_IPSET_UPDATE:
            entries[index] = IpsetEntry(op.cidr, op.nomatch)
        else:
            del entries[index]
    else:
        raise PlanError(f"unknown operation {op.kind}")
    return new


class _Builder:
    def __init__(self, state: CurrentFirewall):
        self.state = state.copy()
        self.ops: list[Op] = []
        self.logical = 0

    def emit(self, op: Op) -> None:
        self.state = simulate(self.state, op)
        self.ops.append(op)

    def insert_rule(self, params: dict[str, Any], final_index: int) -> None:
        """Create disabled at the top, move to ``final_index``, then enable if wanted."""
        wanted_enable = int(params.get("enable", 1))
        self.emit(Op(OP_RULE_CREATE, params={**params, "enable": 0}))
        if final_index > 0:
            self.emit(Op(OP_RULE_MOVE, pos=0, moveto=final_index + 1))
        if wanted_enable:
            self.emit(Op(OP_RULE_UPDATE, pos=final_index, params={"enable": 1}))

    def update_rule(self, pos: int, want: dict[str, Any]) -> bool:
        have = normalise_rule(self.state.rules[pos])
        target = normalise_rule(want)
        if have == target:
            return False
        changed = {key: want[key] for key in want if key in RULE_PROPERTIES and target.get(key) != have.get(key)}
        delete = tuple(key for key in DELETABLE_RULE_PROPERTIES if key in have and key not in target)
        self.emit(Op(OP_RULE_UPDATE, pos=pos, params=changed, delete=delete))
        return True

    def rewrite_region(self, start: int, target: list[dict[str, Any]]) -> None:
        """Make ``rules[start:]`` equal ``target``; counts logical changes in ``self.logical``."""
        wanted = {rule["comment"] for rule in target}
        seen: set[str] = set()
        doomed = []
        for index in range(start, len(self.state.rules)):
            name = marker(self.state.rules[index])
            if name is None or name not in wanted or name in seen:
                doomed.append(index)
            else:
                seen.add(name)
        for index in reversed(doomed):
            self.emit(Op(OP_RULE_DELETE, pos=index))
            self.logical += 1
        for offset, want in enumerate(target):
            final = start + offset
            found = next(
                (i for i in range(final, len(self.state.rules)) if marker(self.state.rules[i]) == want["comment"]),
                None,
            )
            if found is None:
                self.insert_rule(want, final)
                self.logical += 1
                continue
            if found != final:
                self.emit(Op(OP_RULE_MOVE, pos=found, moveto=final))
                self.logical += 1
            if self.update_rule(final, want):
                self.logical += 1


def _options_delta(current: dict[str, Any], desired: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    have = normalise_options(current)
    changed = {key: value for key, value in desired.items() if key != "enable" and have.get(key) != _str(value)}
    delete = tuple(sorted(key for key in have if key not in OPTION_KEYS))
    return changed, delete


def _baseline_prefix(rules: list[dict[str, Any]], desired: list[dict[str, Any]], baseline_count: int) -> int:
    prefix = 0
    while (
        prefix < baseline_count
        and prefix < len(rules)
        and normalise_rule(rules[prefix]) == normalise_rule(desired[prefix])
    ):
        prefix += 1
    return prefix


def _direction(rules: list[dict[str, Any]], direction: str) -> list[dict[str, str]]:
    return [normalise_rule(rule) for rule in rules if _str(rule.get("type", "")).lower() == direction]


def plan(current: CurrentFirewall, desired: RenderedFirewall) -> list[Op]:
    builder = _Builder(current)
    desired_rules = [rule.params() for rule in desired.rules]
    current_enable = _str(current.options.get("enable", 0)) == "1"
    desired_enable = int(desired.options["enable"]) == 1
    live = current_enable and desired_enable

    if current_enable and not desired_enable:
        builder.emit(Op(OP_OPTIONS, params={"enable": 0}))

    for name in sorted(desired.ipsets):
        if name not in builder.state.ipsets:
            builder.emit(Op(OP_IPSET_CREATE, ipset=name))
        have = {entry.cidr: entry for entry in builder.state.ipsets[name]}
        for entry in desired.ipsets[name]:
            existing = have.get(entry.cidr)
            if existing is None:
                builder.emit(Op(OP_IPSET_ADD, ipset=name, cidr=entry.cidr, nomatch=entry.nomatch))
            elif existing.nomatch != entry.nomatch:
                builder.emit(Op(OP_IPSET_UPDATE, ipset=name, cidr=entry.cidr, nomatch=entry.nomatch))

    option_changes, option_deletes = _options_delta(builder.state.options, desired.options)
    options_differ = bool(option_changes or option_deletes)

    trial = _Builder(builder.state)
    trial.rewrite_region(0, desired_rules)
    guarded = live and trial.logical + (1 if options_differ else 0) > 1

    if not guarded:
        builder.rewrite_region(0, desired_rules)
        if options_differ:
            builder.emit(Op(OP_OPTIONS, params=option_changes, delete=option_deletes))
    else:
        prefix = _baseline_prefix(builder.state.rules, desired_rules, desired.baseline_rule_count)
        guards = [{"type": "in", "action": "DROP", "comment": GUARD_MARKER, "enable": 1}]
        if _direction(builder.state.rules[prefix:], "out") != _direction(desired_rules[prefix:], "out"):
            guards.append({"type": "out", "action": "DROP", "comment": GUARD_MARKER, "enable": 1})
        for offset, guard in enumerate(guards):
            builder.insert_rule(guard, prefix + offset)
        builder.rewrite_region(prefix + len(guards), desired_rules[prefix:])
        if options_differ:
            builder.emit(Op(OP_OPTIONS, params=option_changes, delete=option_deletes))
        for offset in reversed(range(len(guards))):
            builder.emit(Op(OP_RULE_DELETE, pos=prefix + offset))

    for name in sorted(builder.state.ipsets):
        if name not in desired.ipsets:
            continue
        keep = {entry.cidr for entry in desired.ipsets[name]}
        for entry in list(builder.state.ipsets[name]):
            if entry.cidr not in keep:
                builder.emit(Op(OP_IPSET_REMOVE, ipset=name, cidr=entry.cidr))
    for name in sorted(builder.state.ipsets):
        if name not in desired.ipsets:
            builder.emit(Op(OP_IPSET_DELETE, ipset=name))

    if desired_enable and not current_enable:
        builder.emit(Op(OP_OPTIONS, params={"enable": 1}))
    return builder.ops


def matches(current: CurrentFirewall, desired: RenderedFirewall) -> bool:
    return not plan(current, desired)


def differences(current: CurrentFirewall, desired: RenderedFirewall) -> list[str]:
    """Readable differences, for drift reports."""
    out: list[str] = []
    have = normalise_options(current.options)
    for key, value in desired.options.items():
        if have.get(key) != _str(value):
            out.append(f"option {key}: {have.get(key)!r} instead of {_str(value)!r}")
    for key in sorted(set(have) - set(OPTION_KEYS)):
        out.append(f"option {key} is not managed by Rackflow")
    for name in sorted(set(desired.ipsets) | set(current.ipsets)):
        want = {(e.cidr, e.nomatch) for e in desired.ipsets.get(name, [])}
        got = {(e.cidr, e.nomatch) for e in current.ipsets.get(name, [])}
        if name not in current.ipsets:
            out.append(f"IP set {name} is missing")
        elif name not in desired.ipsets:
            out.append(f"IP set {name} is not managed by Rackflow")
        elif want != got:
            out.append(f"IP set {name} entries differ")
    desired_rules = [normalise_rule(rule.params()) for rule in desired.rules]
    current_rules = [normalise_rule(rule) for rule in current.rules]
    for index, rule in enumerate(current.rules):
        if marker(rule) is None:
            out.append(f"rule at position {index} has no Rackflow marker")
    if desired_rules != current_rules:
        out.append(f"rules differ ({len(current_rules)} on Proxmox, {len(desired_rules)} rendered)")
    return out


__all__ = [
    "CurrentFirewall",
    "Op",
    "PlanError",
    "differences",
    "marker",
    "matches",
    "normalise_rule",
    "plan",
    "simulate",
]
