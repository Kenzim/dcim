"""Write a rendered VM firewall to Proxmox, check it for drift, and check cluster readiness.

``apply`` renders, reads, plans and executes; after every write it re-reads the object it touched,
so the next write carries a fresh digest and a concurrent edit is noticed at once. On a digest
conflict (or any state other than the one simulated) it re-reads and re-plans, at most
``MAX_REPLANS`` times. It records the outcome on the ``ServiceFirewall`` row and leaves the commit
to the caller. Given the applier's ``claim``, it renews the claim before every write and records
the outcome only while the claim is still the applier's (``CLAIM_LOST`` otherwise). ``check`` and
``readiness`` only read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from sqlalchemy.orm import Session

from app.models.vm_firewall import FirewallNicFlag, FirewallState, ServiceFirewall
from app.services.vm_firewall.errors import FirewallNotFound
from app.services.vm_firewall.planner import (
    OP_IPSET_ADD,
    OP_IPSET_CREATE,
    OP_IPSET_DELETE,
    OP_IPSET_REMOVE,
    OP_IPSET_UPDATE,
    OP_OPTIONS,
    OP_RULE_CREATE,
    OP_RULE_DELETE,
    OP_RULE_MOVE,
    OP_RULE_UPDATE,
    CurrentFirewall,
    Op,
    differences,
    matches,
    normalise_options,
    normalise_rule,
    plan,
    simulate,
)
from app.services.vm_firewall.proxmox_fw import (
    ClusterReader,
    DigestConflict,
    ProxmoxApiError,
    VmFirewallClient,
    VmMissing,
    fingerprint,
    net_firewall_flag,
)
from app.services.vm_firewall.render import RenderedFirewall, render
from app.services.vm_firewall.service import observe_net0_bridge

logger = logging.getLogger(__name__)

MAX_REPLANS = 3
BACKOFF_MIN_SECONDS = 30
BACKOFF_MAX_SECONDS = 600
# ``ApplyResult.state`` of an apply stopped because another applier took the claim; never stored
# on the row.
CLAIM_LOST = "claim_lost"


class ConcurrentChange(DigestConflict):
    """Proxmox holds something other than what the last write should have produced."""

    def __init__(self, message: str):
        super().__init__(409, message, method="GET", path="")


class ClaimLost(Exception):
    """The applier's claim on the row was taken by another applier; nothing more may be written."""


class ApplyClaim(Protocol):
    """The applier's claim on the row (``reconcile.Claim``)."""

    def renew(self) -> bool:
        """Extend the claim from now; false when it is no longer this applier's. Commits on its own."""

    def confirm(self, db: Session) -> bool:
        """Whether the claim is still this applier's, locked in ``db`` until it commits."""


def _renew(claim: Optional[ApplyClaim]) -> None:
    if claim is not None and not claim.renew():
        raise ClaimLost("the claim on this VM was taken by another applier")


@dataclass
class ApplyResult:
    service_id: int
    ok: bool
    state: str
    revision: Optional[int] = None
    desired_hash: Optional[str] = None
    ops: int = 0
    replans: int = 0
    nic_flag: str = FirewallNicFlag.UNKNOWN.value
    error: Optional[str] = None
    # From the rendered document: protections not applied and why (e.g. ipv6_ra_bridge_needs_prefix).
    reasons: list[str] = field(default_factory=list)


@dataclass
class DriftReport:
    service_id: int
    in_sync: bool
    differences: list[str] = field(default_factory=list)
    digest_matches: bool = False
    nic_flag: str = FirewallNicFlag.UNKNOWN.value
    desired_hash: Optional[str] = None
    vm_missing: bool = False


@dataclass
class Readiness:
    ready: bool
    reasons: list[str] = field(default_factory=list)
    datacenter: dict[str, Any] = field(default_factory=dict)
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "reasons": list(self.reasons), "datacenter": self.datacenter, "nodes": self.nodes}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _send(client: VmFirewallClient, op: Op, digests: dict[str, Optional[str]]) -> None:
    if op.kind == OP_OPTIONS:
        await client.set_options(op.params, delete=op.delete, digest=digests.get("options"))
    elif op.kind == OP_RULE_CREATE:
        await client.create_rule(op.params)
    elif op.kind == OP_RULE_UPDATE:
        await client.update_rule(op.pos, op.params, delete=op.delete, digest=digests.get("rules"))
    elif op.kind == OP_RULE_MOVE:
        await client.update_rule(op.pos, {}, moveto=op.moveto, digest=digests.get("rules"))
    elif op.kind == OP_RULE_DELETE:
        await client.delete_rule(op.pos, digest=digests.get("rules"))
    elif op.kind == OP_IPSET_CREATE:
        await client.create_ipset(op.ipset, digest=digests.get("ipsets"))
    elif op.kind == OP_IPSET_DELETE:
        await client.delete_ipset(op.ipset)
    elif op.kind == OP_IPSET_ADD:
        await client.add_ipset_entry(op.ipset, op.cidr, nomatch=op.nomatch)
    elif op.kind == OP_IPSET_UPDATE:
        await client.update_ipset_entry(op.ipset, op.cidr, nomatch=op.nomatch, digest=digests.get(f"ipset:{op.ipset}"))
    elif op.kind == OP_IPSET_REMOVE:
        await client.remove_ipset_entry(op.ipset, op.cidr, digest=digests.get(f"ipset:{op.ipset}"))
    else:
        raise ValueError(f"unknown operation {op.kind}")


async def _refresh(client: VmFirewallClient, op: Op, expected: CurrentFirewall) -> CurrentFirewall:
    """Re-read what ``op`` touched, check it equals ``expected`` and take its new digest."""
    state = expected.copy()
    if op.kind == OP_OPTIONS:
        options = await client.get_options()
        state.digests["options"] = options.pop("digest", None)
        if normalise_options(options) != normalise_options(expected.options):
            raise ConcurrentChange("firewall options changed during apply")
        state.options = options
    elif op.kind in (OP_RULE_CREATE, OP_RULE_UPDATE, OP_RULE_MOVE, OP_RULE_DELETE):
        rules, digest = await client.get_rules()
        state.digests["rules"] = digest
        if [normalise_rule(r) for r in rules] != [normalise_rule(r) for r in expected.rules]:
            raise ConcurrentChange("firewall rules changed during apply")
        state.rules = rules
    elif op.kind in (OP_IPSET_CREATE, OP_IPSET_DELETE):
        names, digest = await client.list_ipsets()
        state.digests["ipsets"] = digest
        if sorted(names) != sorted(expected.ipsets):
            raise ConcurrentChange("IP sets changed during apply")
        if op.kind == OP_IPSET_CREATE:
            state.digests[f"ipset:{op.ipset}"] = None
        else:
            state.digests.pop(f"ipset:{op.ipset}", None)
    else:
        entries, digest = await client.get_ipset(op.ipset)
        state.digests[f"ipset:{op.ipset}"] = digest
        if set(entries) != set(expected.ipsets.get(op.ipset, [])):
            raise ConcurrentChange(f"IP set {op.ipset} changed during apply")
        state.ipsets[op.ipset] = entries
    return state


async def execute(
    client: VmFirewallClient, ops: list[Op], current: CurrentFirewall, *, claim: Optional[ApplyClaim] = None
) -> CurrentFirewall:
    state = current
    for op in ops:
        expected = simulate(state, op)
        _renew(claim)
        await _send(client, op, state.digests)
        state = await _refresh(client, op, expected)
    return state


@dataclass
class _Converged:
    final: CurrentFirewall
    nic_flag: FirewallNicFlag
    ops: int
    replans: int


async def converge(
    client: VmFirewallClient,
    desired: RenderedFirewall,
    *,
    max_replans: int = MAX_REPLANS,
    claim: Optional[ApplyClaim] = None,
) -> _Converged:
    """Make Proxmox hold ``desired``; when enabling a VM whose ``net0`` has no firewall flag, write
    everything with ``enable: 0`` first, flip the flag, then enable (section 5.2). Raises
    ``ClaimLost`` before a write when ``claim`` can no longer be renewed."""
    want_enable = int(desired.options["enable"]) == 1
    replans = 0
    total_ops = 0
    while True:
        try:
            current = await client.read()
            config = await client.get_config()
            flag_on = net_firewall_flag(config.get("net0"))
            if want_enable and not flag_on:
                ops = plan(current, desired.with_options(enable=0))
                current = await execute(client, ops, current, claim=claim)
                total_ops += len(ops)
                _renew(claim)
                await client.set_net0_firewall_flag(True)
                total_ops += 1
                flag_on = True
            ops = plan(current, desired)
            current = await execute(client, ops, current, claim=claim)
            total_ops += len(ops)
            final = await client.read()
            if not matches(final, desired):
                raise ConcurrentChange("firewall differs from the rendered document after apply")
            if flag_on:
                nic_flag = FirewallNicFlag.PENDING_REBOOT if await client.net0_pending() else FirewallNicFlag.ON
            else:
                nic_flag = FirewallNicFlag.OFF
            return _Converged(final=final, nic_flag=nic_flag, ops=total_ops, replans=replans)
        except DigestConflict:
            if replans >= max_replans:
                raise
            replans += 1
            logger.info("[vm_firewall] digest conflict on VM %s, re-planning (%d)", client.vmid, replans)


def _backoff(row: ServiceFirewall, now: datetime) -> datetime:
    since = row.failing_since
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    elapsed = (now - since).total_seconds() if since is not None else 0
    delay = min(BACKOFF_MAX_SECONDS, max(BACKOFF_MIN_SECONDS, elapsed))
    return now + timedelta(seconds=delay)


def record_failure(
    db: Session,
    row: ServiceFirewall,
    exc: Exception,
    *,
    now: datetime,
    revision: Optional[int] = None,
    desired: Optional[RenderedFirewall] = None,
) -> ApplyResult:
    """Record a failed apply or read on the row: ``vm_missing`` for a VM Proxmox does not have,
    else ``error`` with the backoff of section 6.2. Flushes; the caller commits."""
    revision = row.revision if revision is None else revision
    if isinstance(exc, VmMissing):
        row.state = FirewallState.VM_MISSING
        row.last_error = str(exc)[:2000]
        db.flush()
        return ApplyResult(row.service_id, ok=False, state=FirewallState.VM_MISSING.value, revision=revision,
                           error=row.last_error)
    logger.warning("[vm_firewall] apply failed for service %s: %s", row.service_id, exc)
    row.state = FirewallState.ERROR
    row.last_error = (str(exc) or exc.__class__.__name__)[:2000]
    row.next_attempt_at = _backoff(row, now)
    if row.failing_since is None:
        row.failing_since = now
    db.flush()
    return ApplyResult(
        row.service_id,
        ok=False,
        state=FirewallState.ERROR.value,
        revision=revision,
        desired_hash=desired.desired_hash if desired is not None else None,
        error=row.last_error,
    )


async def apply(
    db: Session,
    service_id: int,
    client: VmFirewallClient,
    *,
    now: Optional[datetime] = None,
    vm_config: Optional[dict[str, Any]] = None,
    claim: Optional[ApplyClaim] = None,
) -> ApplyResult:
    """``vm_config`` is the VM config the caller has just read through ``client`` (the worker reads
    it for the eligibility check); without it apply reads it itself. With ``claim``, a lost claim
    stops the apply before its next write and records nothing: the row's unflushed changes are
    discarded and the result has state ``CLAIM_LOST``."""
    row = db.get(ServiceFirewall, service_id)
    if row is None:
        raise FirewallNotFound(f"service {service_id} has no firewall", code="firewall_not_found")
    now = now or _now()
    row.last_checked_at = now
    if not row.managed or row.excluded:
        row.state = FirewallState.UNMANAGED
        db.flush()
        return ApplyResult(service_id, ok=True, state=FirewallState.UNMANAGED.value, revision=row.revision)

    def failed(exc: Exception, revision: int, desired: Optional[RenderedFirewall]) -> ApplyResult:
        return record_failure(db, row, exc, now=now, revision=revision, desired=desired)

    def claim_lost(revision: int, desired: RenderedFirewall) -> ApplyResult:
        logger.info("[vm_firewall] claim on service %s lost during apply; left for the next tick", service_id)
        db.expire(row)
        return ApplyResult(service_id, ok=False, state=CLAIM_LOST, revision=revision,
                           desired_hash=desired.desired_hash, error="the firewall is being applied by another applier")

    # Render depends on net0's bridge (IPv6 router-advert rule), so observe it first; a change
    # bumps the revision and this apply renders the new one.
    if vm_config is None:
        try:
            vm_config = await client.get_config()
        except Exception as exc:  # Proxmox errors, transport failures
            return failed(exc, row.revision, None)
    observe_net0_bridge(row, vm_config)

    revision = row.revision
    desired = render(db, service_id, now=now)
    row.desired_hash = desired.desired_hash
    try:
        result = await converge(client, desired, claim=claim)
    except ClaimLost:
        return claim_lost(revision, desired)
    except Exception as exc:  # Proxmox errors, transport failures, crashes mid-apply
        return failed(exc, revision, desired)
    if claim is not None and not claim.confirm(db):
        return claim_lost(revision, desired)

    if int(desired.options["enable"]) != 1:
        state = FirewallState.OFF
    elif result.nic_flag == FirewallNicFlag.PENDING_REBOOT:
        state = FirewallState.PENDING_REBOOT
    else:
        state = FirewallState.APPLIED
    row.applied_revision = revision
    row.applied_hash = desired.desired_hash
    row.applied_digest = fingerprint(result.final)
    row.nic_flag = result.nic_flag
    row.state = state
    row.last_error = None
    row.failing_since = None
    row.next_attempt_at = None
    row.last_applied_at = now
    db.flush()
    return ApplyResult(
        service_id,
        ok=True,
        state=state.value,
        revision=revision,
        desired_hash=desired.desired_hash,
        ops=result.ops,
        replans=result.replans,
        nic_flag=result.nic_flag.value,
        reasons=list(desired.reasons),
    )


async def check(db: Session, service_id: int, client: VmFirewallClient) -> DriftReport:
    """Compare Proxmox with the rendered document; writes nothing anywhere."""
    row = db.get(ServiceFirewall, service_id)
    if row is None:
        raise FirewallNotFound(f"service {service_id} has no firewall", code="firewall_not_found")
    desired = render(db, service_id)
    try:
        current = await client.read()
        config = await client.get_config()
    except VmMissing:
        return DriftReport(service_id, in_sync=False, differences=["VM not found on Proxmox"], vm_missing=True)
    found = differences(current, desired)
    flag_on = net_firewall_flag(config.get("net0"))
    if int(desired.options["enable"]) == 1 and not flag_on:
        found.append("net0 has no firewall flag")
    if flag_on:
        nic_flag = FirewallNicFlag.PENDING_REBOOT if await client.net0_pending() else FirewallNicFlag.ON
    else:
        nic_flag = FirewallNicFlag.OFF
    return DriftReport(
        service_id,
        in_sync=not found,
        differences=found,
        digest_matches=row.applied_digest is not None and fingerprint(current) == row.applied_digest,
        nic_flag=nic_flag.value,
        desired_hash=desired.desired_hash,
    )


def _on(value: Any) -> bool:
    return value in (1, True, "1")


async def readiness(cluster: ClusterReader) -> Readiness:
    """Section 9.2: datacenter firewall on, firewall services running on every node, and every node
    on the same backend (``nftables``)."""
    reasons: list[str] = []
    try:
        options = await cluster.cluster_options()
    except ProxmoxApiError as exc:
        return Readiness(ready=False, reasons=[f"datacenter firewall options unreadable: {exc.message}"])
    datacenter = {key: options.get(key) for key in ("enable", "policy_in", "policy_out", "ebtables")}
    if not _on(options.get("enable")):
        reasons.append("datacenter firewall disabled")

    nodes: dict[str, dict[str, Any]] = {}
    for node in await cluster.nodes():
        info: dict[str, Any] = {}
        try:
            node_options = await cluster.node_options(node)
        except ProxmoxApiError as exc:
            reasons.append(f"node {node}: firewall options unreadable: {exc.message}")
            nodes[node] = {"error": exc.message}
            continue
        nftables = _on(node_options.get("nftables"))
        info["enable"] = node_options.get("enable", 1)
        info["nftables"] = nftables
        services = ["pve-firewall"] + (["proxmox-firewall"] if nftables else [])
        for service in services:
            try:
                state = (await cluster.service_state(node, service)).get("state")
            except ProxmoxApiError as exc:
                state = None
                reasons.append(f"node {node}: {service} state unreadable: {exc.message}")
            info[service] = state
            if state is not None and state != "running":
                reasons.append(f"node {node}: {service} not running")
        nodes[node] = info

    backends = {name: info["nftables"] for name, info in nodes.items() if "nftables" in info}
    if len(set(backends.values())) > 1:
        on = sorted(name for name, value in backends.items() if value)
        off = sorted(name for name, value in backends.items() if not value)
        minority = on if len(on) <= len(off) else off
        reasons.append(
            "nodes differ in the nftables setting: "
            + ", ".join(f"{name} ({'nftables' if backends[name] else 'iptables'})" for name in minority)
        )
    return Readiness(ready=not reasons, reasons=reasons, datacenter=datacenter, nodes=nodes)
