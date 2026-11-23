"""Render the canonical Proxmox firewall document of one VM (docs/vm-firewall.md section 4.5).

``render`` only reads the database. Rule order is: baseline rules (global, location, product,
groups expanded), then generated baseline rules (SMTP block, management block), then the client's
rules with groups expanded in place. Proxmox sees only validated fields and ``rf:`` markers in
comments; names, descriptions and comments stay in Rackflow.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.proxmox_inventory import ProxmoxCluster
from app.models.reseller import ServiceBilling
from app.models.service import Service
from app.models.service_vm import ServiceVm
from app.models.vm_firewall import (
    FirewallBaseline,
    FirewallBaselineScope,
    FirewallIpset,
    FirewallIpsetPurpose,
    FirewallLocationSettings,
    FirewallObjectScope,
    FirewallRule,
    FirewallRuleKind,
    ServiceFirewall,
)

IPFILTER_SET = "ipfilter-net0"
GUARD_MARKER = "rf:guard"
MARKER_PREFIX = "rf:"
SMTP_PORTS = ("25",)

DEFAULT_SETTINGS: dict[str, Any] = {
    "anti_spoof": True,
    "macfilter": True,
    # None (no baseline sets it) means allow; per-service blocks come from ``admin_overrides``.
    "smtp_out": None,
    "block_management": True,
    # None: every admin IP set with purpose ``management``.
    "management_ipset_ids": None,
    "client_may_disable": False,
}
OPTION_KEYS = (
    "enable", "policy_in", "policy_out", "ipfilter", "macfilter", "dhcp", "ndp", "radv",
    "log_level_in", "log_level_out",
)
RULE_KEYS = ("type", "action", "proto", "dport", "sport", "source", "dest", "icmp-type", "enable", "log", "comment")


def ipset_name(ipset_id: int) -> str:
    return f"rf-s{ipset_id}"


@dataclass(frozen=True)
class RenderedRule:
    """One Proxmox rule. ``params`` uses Proxmox's parameter names."""

    type: str
    action: str
    comment: str
    enable: int = 1
    proto: Optional[str] = None
    dport: Optional[str] = None
    sport: Optional[str] = None
    source: Optional[str] = None
    dest: Optional[str] = None
    icmp_type: Optional[str] = None
    log: Optional[str] = None

    def params(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "action": self.action, "enable": self.enable, "comment": self.comment}
        for key, value in (
            ("proto", self.proto),
            ("dport", self.dport),
            ("sport", self.sport),
            ("source", self.source),
            ("dest", self.dest),
            ("icmp-type", self.icmp_type),
            ("log", self.log),
        ):
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class IpsetEntry:
    cidr: str
    nomatch: bool = False


@dataclass
class RenderedFirewall:
    service_id: int
    options: dict[str, Any]
    ipsets: dict[str, list[IpsetEntry]]
    rules: list[RenderedRule]
    baseline_rule_count: int
    enforced: bool
    active: bool
    settings: dict[str, Any] = field(default_factory=dict)
    desired_hash: str = ""

    def canonical(self) -> str:
        document = {
            "options": {key: self.options[key] for key in sorted(self.options)},
            "ipsets": {
                name: [[entry.cidr, int(entry.nomatch)] for entry in self.ipsets[name]]
                for name in sorted(self.ipsets)
            },
            "rules": [rule.params() for rule in self.rules],
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    def with_options(self, **options: Any) -> "RenderedFirewall":
        """A copy with some options replaced (apply writes ``enable: 0`` first when flipping ``net0``)."""
        copy = dataclasses.replace(self, options={**self.options, **options})
        copy.desired_hash = hashlib.sha256(copy.canonical().encode()).hexdigest()
        return copy


@dataclass
class RenderContext:
    """What the renderer reads, loaded once; the service layer reuses it for limit checks."""

    service: Service
    vm: Optional[ServiceVm]
    firewall: Optional[ServiceFirewall]
    location_id: Optional[int]
    product_id: Optional[int]
    location_settings: Optional[FirewallLocationSettings]
    baselines: list[FirewallBaseline]
    settings: dict[str, Any]


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def load_context(db: Session, service_id: int) -> RenderContext:
    service = db.get(Service, service_id)
    if service is None:
        raise LookupError(f"service {service_id} not found")
    vm = db.get(ServiceVm, service_id)
    firewall = db.get(ServiceFirewall, service_id)
    location_id = None
    if vm is not None and vm.proxmox_cluster_id is not None:
        cluster = db.get(ProxmoxCluster, vm.proxmox_cluster_id)
        location_id = cluster.location_id if cluster is not None else None
    product_id = db.execute(
        select(ServiceBilling.product_id).where(ServiceBilling.service_id == service_id)
    ).scalar_one_or_none()
    location_settings = db.get(FirewallLocationSettings, location_id) if location_id is not None else None
    baselines = baselines_for(db, location_id, product_id)
    settings = merge_settings([b.settings for b in baselines], firewall.admin_overrides if firewall else None)
    return RenderContext(
        service=service,
        vm=vm,
        firewall=firewall,
        location_id=location_id,
        product_id=product_id,
        location_settings=location_settings,
        baselines=baselines,
        settings=settings,
    )


def baselines_for(db: Session, location_id: Optional[int], product_id: Optional[int]) -> list[FirewallBaseline]:
    """Enabled baselines that apply, in merge order: global, location, product."""
    out: list[FirewallBaseline] = []
    scopes = [(FirewallBaselineScope.GLOBAL, None)]
    if location_id is not None:
        scopes.append((FirewallBaselineScope.LOCATION, location_id))
    if product_id is not None:
        scopes.append((FirewallBaselineScope.PRODUCT, product_id))
    for scope_type, scope_id in scopes:
        query = select(FirewallBaseline).where(
            FirewallBaseline.scope_type == scope_type, FirewallBaseline.enabled.is_(True)
        )
        query = query.where(
            FirewallBaseline.scope_id.is_(None) if scope_id is None else FirewallBaseline.scope_id == scope_id
        )
        out.extend(db.execute(query.order_by(FirewallBaseline.id)).scalars())
    return out


def merge_settings(layers: Iterable[Optional[dict]], overrides: Optional[dict]) -> dict[str, Any]:
    """Most specific non-null value wins; admin overrides of the service come last."""
    merged = dict(DEFAULT_SETTINGS)
    for layer in layers:
        for key, value in (layer or {}).items():
            if key in merged and value is not None:
                merged[key] = value
    extra: list[str] = []
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key == "extra_ipfilter":
            extra = list(value)
        elif key in merged:
            merged[key] = value
    merged["extra_ipfilter"] = extra
    return merged


def _rules_of(db: Session, column, owner_id: int) -> list[FirewallRule]:
    return list(
        db.execute(select(FirewallRule).where(column == owner_id).order_by(FirewallRule.position, FirewallRule.id)).scalars()
    )


def _join(cidrs: Optional[Sequence[str]]) -> Optional[str]:
    return ",".join(cidrs) if cidrs else None


def render_rule(row: FirewallRule, marker: str, *, enabled: bool = True) -> RenderedRule:
    """One stored rule (kind ``rule``) as a Proxmox rule; the VM is always the local end."""
    direction = _value(row.direction)
    remote = f"+{ipset_name(row.remote_ipset_id)}" if row.remote_ipset_id else _join(row.remote_cidrs)
    local = _join(row.local_cidrs)
    source, dest = (remote, local) if direction == "in" else (local, remote)
    ip_version = _value(row.ip_version)
    if ip_version in ("4", "6") and source is None and dest is None:
        source = "0.0.0.0/0" if ip_version == "4" else "::/0"
    return RenderedRule(
        type=direction,
        action=_value(row.action),
        comment=marker,
        enable=1 if (enabled and row.enabled) else 0,
        proto=row.protocol,
        dport=row.ports,
        sport=row.source_ports,
        source=source,
        dest=dest,
        icmp_type=row.icmp_type,
        log="info" if row.log else None,
    )


def _expand(db: Session, entries: Sequence[FirewallRule], prefix: str, group_prefix: Optional[str]) -> list[RenderedRule]:
    """Expand an ordered list; ``group_prefix`` None means group markers nest under the entry id."""
    out: list[RenderedRule] = []
    for entry in entries:
        if _value(entry.kind) == FirewallRuleKind.GROUP.value:
            head = f"{MARKER_PREFIX}{prefix}{entry.id}" if group_prefix is None else f"{MARKER_PREFIX}{group_prefix}{entry.ref_group_id}"
            for rule in _rules_of(db, FirewallRule.group_id, entry.ref_group_id):
                if _value(rule.kind) != FirewallRuleKind.RULE.value:
                    continue
                out.append(render_rule(rule, f"{head}.{rule.id}", enabled=bool(entry.enabled)))
        else:
            out.append(render_rule(entry, f"{MARKER_PREFIX}{prefix}{entry.id}"))
    return out


def management_ipset_ids(db: Session, settings: dict[str, Any]) -> list[int]:
    if not settings.get("block_management"):
        return []
    ids = settings.get("management_ipset_ids")
    query = select(FirewallIpset.id).where(FirewallIpset.scope == FirewallObjectScope.ADMIN)
    if ids is None:
        query = query.where(FirewallIpset.purpose == FirewallIpsetPurpose.MANAGEMENT)
    else:
        query = query.where(FirewallIpset.id.in_(list(ids) or [-1]))
    return sorted(db.execute(query).scalars())


def baseline_rules(db: Session, ctx: RenderContext) -> list[RenderedRule]:
    """Admin baseline rules followed by the generated ones."""
    out: list[RenderedRule] = []
    for baseline in ctx.baselines:
        out.extend(_expand(db, _rules_of(db, FirewallRule.baseline_id, baseline.id), "b", None))
    if ctx.settings.get("smtp_out") == "block":
        for port in SMTP_PORTS:
            out.append(RenderedRule(type="out", action="DROP", comment=f"{MARKER_PREFIX}smtp{port}", proto="tcp", dport=port))
    for ipset_id in management_ipset_ids(db, ctx.settings):
        if not _entries_of(db, ipset_id):
            continue
        ref = f"+{ipset_name(ipset_id)}"
        out.append(RenderedRule(type="out", action="DROP", comment=f"{MARKER_PREFIX}mgmt-out.{ipset_id}", dest=ref))
        out.append(RenderedRule(type="in", action="DROP", comment=f"{MARKER_PREFIX}mgmt-in.{ipset_id}", source=ref))
    return out


def client_rules(db: Session, service_id: int) -> list[RenderedRule]:
    return _expand(db, _rules_of(db, FirewallRule.service_id, service_id), "c", "g")


def _entries_of(db: Session, ipset_id: int) -> list[IpsetEntry]:
    ipset = db.get(FirewallIpset, ipset_id)
    if ipset is None:
        return []
    return sorted((IpsetEntry(e.cidr, bool(e.nomatch)) for e in ipset.entries), key=lambda e: e.cidr)


def _ipfilter_entries(ctx: RenderContext) -> list[IpsetEntry]:
    cidrs: list[str] = []
    allocation = ctx.vm.vm_ip_allocation if ctx.vm is not None else None
    if allocation is not None and allocation.ip_address:
        cidrs.append(allocation.ip_address)
    cidrs.extend(ctx.settings.get("extra_ipfilter") or [])
    if ctx.location_settings is not None:
        cidrs.extend(ctx.location_settings.ipv6_allowed_prefixes or [])
    return sorted({IpsetEntry(cidr) for cidr in cidrs}, key=lambda e: e.cidr)


def _referenced_ipsets(rules: Iterable[RenderedRule]) -> list[int]:
    ids = set()
    for rule in rules:
        for side in (rule.source, rule.dest):
            if side and side.startswith("+rf-s"):
                ids.add(int(side[len("+rf-s"):]))
    return sorted(ids)


def render(db: Session, service_id: int) -> RenderedFirewall:
    ctx = load_context(db, service_id)
    return render_context(db, ctx)


def render_context(db: Session, ctx: RenderContext) -> RenderedFirewall:
    firewall = ctx.firewall
    client_enabled = bool(firewall.client_enabled) if firewall is not None else False
    location_enabled = bool(ctx.location_settings and ctx.location_settings.enabled)
    excluded = bool(firewall.excluded) if firewall is not None else False
    active = location_enabled and not excluded

    baseline = baseline_rules(db, ctx)
    rules = list(baseline)
    if client_enabled:
        rules.extend(client_rules(db, ctx.service.id))

    ipsets: dict[str, list[IpsetEntry]] = {IPFILTER_SET: _ipfilter_entries(ctx)}
    for ipset_id in _referenced_ipsets(rules):
        ipsets[ipset_name(ipset_id)] = _entries_of(db, ipset_id)

    settings = ctx.settings
    location = ctx.location_settings
    enforce_baseline = bool(firewall.enforce_baseline) if firewall is not None else False
    enforce_all = bool(location and location.enforce_baseline_on_all)
    # A VM enforced only through ``enforce_baseline`` (a new VM, section 4.5a) gets the narrow
    # baseline: anti-spoofing and the MAC filter stay off until the client turns the firewall on,
    # unless the location's ``anti_spoof_new_vms`` says otherwise.
    narrow = (
        enforce_baseline
        and not client_enabled
        and not enforce_all
        and not bool(location and location.anti_spoof_new_vms)
    )
    # An empty ipfilter set would drop all of the VM's IPv4 traffic (section 2), so anti-spoofing
    # needs at least one address.
    anti_spoof = bool(settings.get("anti_spoof")) and bool(ipsets[IPFILTER_SET]) and not narrow
    macfilter = bool(settings.get("macfilter")) and not narrow
    enforced = anti_spoof or macfilter or bool(baseline)
    enable = active and (client_enabled or (enforced and (enforce_baseline or enforce_all)))

    options = {
        "enable": 1 if enable else 0,
        "policy_in": _value(firewall.policy_in) if client_enabled else "ACCEPT",
        "policy_out": _value(firewall.policy_out) if client_enabled else "ACCEPT",
        "ipfilter": 1 if anti_spoof else 0,
        "macfilter": 1 if macfilter else 0,
        "dhcp": 1,
        "ndp": 1,
        "radv": 0,
        "log_level_in": _value(firewall.log_level_in) if client_enabled else "nolog",
        "log_level_out": _value(firewall.log_level_out) if client_enabled else "nolog",
    }
    rendered = RenderedFirewall(
        service_id=ctx.service.id,
        options=options,
        ipsets=ipsets,
        rules=rules,
        baseline_rule_count=len(baseline),
        enforced=enforced,
        active=active,
        settings=settings,
    )
    rendered.desired_hash = hashlib.sha256(rendered.canonical().encode()).hexdigest()
    return rendered
