"""VM firewall domain service: every surface (client, admin, billing, MCP, worker) goes through here.

Pure database logic. Every mutating function takes an ``Actor``, checks it may make the change,
checks the caller's ``revision`` (stale raises ``FirewallChanged``), writes the change, bumps the
revision of every VM whose rendered firewall it changes, and writes the audit row in the same
transaction. Nothing commits; the caller does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models.private_network import PrivateNetworkPool
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.reseller import ServiceBilling
from app.models.service import Service, ServiceType
from app.models.service_vm import ServiceVm
from app.models.vm_firewall import (
    FirewallBaseline,
    FirewallBaselineScope,
    FirewallGroup,
    FirewallIpset,
    FirewallIpsetEntry,
    FirewallIpsetPurpose,
    FirewallLocationSettings,
    FirewallLogLevel,
    FirewallObjectScope,
    FirewallRule,
    FirewallRuleKind,
    FirewallState,
    ServiceFirewall,
)
from app.services.audit_service import AuditService
from app.services.private_network_dhcp import PRESENCE_MAC_PREFIXES
from app.services.vm_firewall import validation as v
from app.services.vm_firewall.errors import (
    FirewallChanged,
    FirewallConflict,
    FirewallForbidden,
    FirewallNotEligible,
    FirewallNotFound,
    FirewallValidationError,
)
from app.services.vm_firewall.presets import get_preset
from app.services.vm_firewall.proxmox_fw import net_bridges
from app.services.vm_firewall.render import baseline_rules, load_context

STORAGE_MAC_PREFIXES = PRESENCE_MAC_PREFIXES
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


# Actors


@dataclass(frozen=True)
class Actor:
    """Who makes a change. ``user_id`` is the staff member, the client, or (for ``billing``) the
    service owner the billing integration acts for."""

    kind: str
    user_id: Optional[int] = None
    integration_id: Optional[int] = None
    impersonated_by: Optional[int] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None

    STAFF = "staff"
    CLIENT = "client"
    BILLING = "billing"
    SYSTEM = "system"

    @classmethod
    def staff(cls, user_id: int, **kw: Any) -> "Actor":
        return cls(cls.STAFF, user_id=user_id, **kw)

    @classmethod
    def client(cls, user_id: int, **kw: Any) -> "Actor":
        return cls(cls.CLIENT, user_id=user_id, **kw)

    @classmethod
    def billing(cls, integration_id: int, owner_user_id: int, **kw: Any) -> "Actor":
        return cls(cls.BILLING, user_id=owner_user_id, integration_id=integration_id, **kw)

    @classmethod
    def system(cls) -> "Actor":
        return cls(cls.SYSTEM)

    @property
    def is_staff(self) -> bool:
        return self.kind in (self.STAFF, self.SYSTEM)

    def audit_fields(self) -> dict[str, Any]:
        return {
            "actor_user_id": self.user_id if self.kind in (self.STAFF, self.CLIENT) else None,
            "impersonated_by": self.impersonated_by,
            "ip": self.ip,
            "user_agent": self.user_agent,
        }

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.user_id is not None:
            out["user_id"] = self.user_id
        if self.integration_id is not None:
            out["integration_id"] = self.integration_id
        return out


def _require_staff(actor: Actor, field_path: Optional[str] = None) -> None:
    if not actor.is_staff:
        raise FirewallForbidden("Only staff can change this", code="admin_only", field=field_path)


def _audit(
    db: Session,
    actor: Actor,
    action: str,
    *,
    resource_type: str,
    resource_id: Any,
    subject_user_id: Optional[int],
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    AuditService.log(
        db,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        subject_user_id=subject_user_id,
        metadata={"actor": actor.describe(), **(metadata or {})},
        **actor.audit_fields(),
    )


# Limits and settings


def get_limits(db: Session) -> v.Limits:
    from app.services.settings_store import get_setting

    return v.Limits(
        max_rules_per_vm=int(get_setting(db, "vm_firewall_max_rules_per_vm")),
        max_rendered_rules=int(get_setting(db, "vm_firewall_max_rendered_rules")),
        max_groups_per_client=int(get_setting(db, "vm_firewall_max_groups_per_client")),
        max_rules_per_group=int(get_setting(db, "vm_firewall_max_rules_per_group")),
        max_groups_per_vm=int(get_setting(db, "vm_firewall_max_groups_per_vm")),
        max_ipsets_per_client=int(get_setting(db, "vm_firewall_max_ipsets_per_client")),
        max_ipset_entries=int(get_setting(db, "vm_firewall_max_ipset_entries")),
        client_logging=bool(get_setting(db, "firewall_client_logging")),
    )


def _bridge_setting(db: Session, key: str) -> set[str]:
    from app.services.settings_store import get_setting

    return {part.strip() for part in str(get_setting(db, key) or "").split(",") if part.strip()}


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


# Service firewall rows


def _service(db: Session, service_id: int) -> Service:
    service = db.get(Service, service_id)
    if service is None:
        raise FirewallNotFound("Service not found", code="service_not_found")
    return service


def _check_service_access(service: Service, actor: Actor) -> None:
    if actor.is_staff:
        return
    if actor.user_id is None or service.owner_user_id != actor.user_id:
        raise FirewallNotFound("Service not found", code="service_not_found")


def get_or_create(db: Session, service_id: int) -> ServiceFirewall:
    """The firewall row of a VM service, created with the defaults on first use."""
    service = _service(db, service_id)
    if _value(service.service_type) != ServiceType.VM.value:
        raise FirewallNotEligible(["not_vm"])
    row = db.get(ServiceFirewall, service_id)
    if row is None:
        row = ServiceFirewall(
            service_id=service_id,
            client_enabled=False,
            policy_in="DROP",
            policy_out="ACCEPT",
            log_level_in="nolog",
            log_level_out="nolog",
            managed=True,
            enforce_baseline=False,
            excluded=False,
            revision=1,
            nic_flag="unknown",
            state=FirewallState.OFF,
            drift_repairs_24h=0,
        )
        db.add(row)
        db.flush()
    return row


def init_for_new_vm(db: Session, service_id: int, actor: Actor) -> ServiceFirewall:
    """Create the firewall row of a VM being provisioned (section 4.5a; called by the provisioning
    step before first boot). In a firewall-enabled location with ``smtp_block_new_vms`` the row gets
    ``enforce_baseline`` and ``admin_overrides.smtp_out = "block"``; elsewhere it is a plain row.
    Idempotent: an existing row is returned unchanged, so a retried step never re-blocks a VM
    that staff have since unblocked."""
    _require_staff(actor)
    service = _service(db, service_id)
    existing = db.get(ServiceFirewall, service_id)
    if existing is not None:
        return existing
    row = get_or_create(db, service_id)
    vm = db.get(ServiceVm, service_id)
    cluster = db.get(ProxmoxCluster, vm.proxmox_cluster_id) if vm and vm.proxmox_cluster_id else None
    location = db.get(FirewallLocationSettings, cluster.location_id) if cluster and cluster.location_id else None
    if location is None or not location.enabled or not location.smtp_block_new_vms:
        return row
    row.enforce_baseline = True
    row.admin_overrides = {"smtp_out": "block"}
    row.state = FirewallState.PENDING
    db.flush()
    _audit(db, actor, "firewall.admin_override_changed", resource_type="service", resource_id=service_id,
           subject_user_id=service.owner_user_id,
           metadata={"before": {}, "after": {"smtp_out": "block"}, "enforce_baseline": True,
                     "reason": "new_vm"})
    return row


def get_firewall(db: Session, service_id: int, actor: Actor) -> ServiceFirewall:
    _check_service_access(_service(db, service_id), actor)
    return get_or_create(db, service_id)


def _locked(db: Session, service_id: int, actor: Actor, revision: Optional[int]) -> tuple[Service, ServiceFirewall]:
    service = _service(db, service_id)
    _check_service_access(service, actor)
    row = get_or_create(db, service_id)
    db.refresh(row, with_for_update=True)
    if revision is None or isinstance(revision, bool) or int(revision) != row.revision:
        raise FirewallChanged(row.revision)
    return service, row


def service_rules(db: Session, service_id: int) -> list[FirewallRule]:
    return list(
        db.execute(
            select(FirewallRule)
            .where(FirewallRule.service_id == service_id)
            .order_by(FirewallRule.position, FirewallRule.id)
        ).scalars()
    )


def _rule_summary(specs: Sequence[v.RuleSpec]) -> list[dict[str, Any]]:
    return [{k: val for k, val in spec.as_dict().items() if val not in (None, False) or k == "enabled"} for spec in specs]


def _bump(row: ServiceFirewall) -> None:
    row.revision = (row.revision or 0) + 1


# Eligibility (section 3.1)


@dataclass
class Eligibility:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    nics_checked: bool = False


def _macs(net: Mapping[str, Optional[str]]) -> list[str]:
    out = [val.lower() for key, val in net.items() if val and _MAC.match(val.lower())]
    return out


def observe_net0_bridge(row: ServiceFirewall, vm_config: Mapping[str, Any]) -> bool:
    """Record net0's bridge from a Proxmox VM config on the row, bumping the revision when it
    changes (render depends on it). Returns whether it changed; the caller commits."""
    bridge = (net_bridges(dict(vm_config)).get("net0") or {}).get("bridge") or None
    if bridge is not None and len(bridge) > 32:
        bridge = None
    if bridge == row.net0_bridge:
        return False
    row.net0_bridge = bridge
    _bump(row)
    return True


def eligibility(db: Session, service_id: int, *, vm_config: Optional[Mapping[str, Any]] = None) -> Eligibility:
    """Reasons a service cannot have a managed firewall. ``vm_config`` is the Proxmox VM config
    (``netN`` strings); without it the NIC checks are skipped and ``nics_checked`` is false. With it,
    net0's bridge is also recorded on an existing firewall row (``observe_net0_bridge``)."""
    reasons: list[str] = []
    service = _service(db, service_id)
    if _value(service.service_type) != ServiceType.VM.value:
        return Eligibility(False, ["not_vm"])
    vm = db.get(ServiceVm, service_id)
    if vm is None or vm.proxmox_cluster_id is None or not vm.proxmox_node_name or vm.proxmox_vmid is None:
        return Eligibility(False, ["no_vm_record"])
    cluster = db.get(ProxmoxCluster, vm.proxmox_cluster_id)
    location_id = cluster.location_id if cluster is not None else None
    location = db.get(FirewallLocationSettings, location_id) if location_id is not None else None
    if location is None or not location.enabled:
        reasons.append("location_disabled")
    row = db.get(ServiceFirewall, service_id)
    if row is not None and row.excluded:
        reasons.append("excluded")

    if vm_config is not None:
        if row is not None:
            observe_net0_bridge(row, vm_config)
        nets = net_bridges(dict(vm_config))
        infrastructure = _bridge_setting(db, "vm_firewall_infrastructure_bridges")
        ra_bridges = _bridge_setting(db, "vm_firewall_ipv6_ra_bridges")
        pool_bridge = None
        if location_id is not None:
            pool_bridge = db.execute(
                select(PrivateNetworkPool.bridge).where(PrivateNetworkPool.location_id == location_id)
            ).scalar_one_or_none()
        for name in sorted(nets, key=lambda key: int(key[3:])):
            net = nets[name]
            bridge = net.get("bridge")
            if net.get("trunks"):
                _add(reasons, "nic_trunk")
            if pool_bridge and bridge == pool_bridge and not net.get("tag"):
                _add(reasons, "private_bridge_trunk")
            if bridge in infrastructure:
                _add(reasons, "infrastructure_bridge")
            if any(mac.startswith(prefix) for mac in _macs(net) for prefix in STORAGE_MAC_PREFIXES):
                _add(reasons, "storage_mac")
        net0 = nets.get("net0")
        if net0 is None:
            _add(reasons, "no_net0")
        elif net0.get("bridge") in ra_bridges and not (location and location.ipv6_allowed_prefixes):
            _add(reasons, "ipv6_ra_bridge_needs_prefix")
    return Eligibility(not reasons, reasons, nics_checked=vm_config is not None)


def _add(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


# Rule validation against the database


def _group_size(db: Session, group_id: int) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(FirewallRule)
            .where(FirewallRule.group_id == group_id, FirewallRule.kind == FirewallRuleKind.RULE)
        ).scalar_one()
    )


def _check_references(
    db: Session,
    specs: Sequence[v.RuleSpec],
    owner_id: Optional[int],
    actor: Actor,
    field_path: str,
    *,
    is_group: bool = False,
) -> None:
    for index, spec in enumerate(specs):
        path = f"{field_path}[{index}]"
        if spec.ref_group_id is not None:
            group = db.get(FirewallGroup, spec.ref_group_id)
            if group is None:
                raise FirewallValidationError("no such security group", code="reference_not_found", field=f"{path}.group_id")
            v.check_reference(group, owner_id, actor_is_staff=actor.is_staff, field_path=f"{path}.group_id")
        if spec.remote_ipset_id is not None:
            ipset = db.get(FirewallIpset, spec.remote_ipset_id)
            if ipset is None:
                raise FirewallValidationError("no such IP set", code="reference_not_found", field=f"{path}.remote_ipset_id")
            staff = actor.is_staff and (not is_group or owner_id is None)
            v.check_reference(ipset, owner_id, actor_is_staff=staff, field_path=f"{path}.remote_ipset_id")


def _expanded(db: Session, specs: Sequence[v.RuleSpec], sizes: Optional[dict[int, int]] = None) -> int:
    total = 0
    for spec in specs:
        if spec.kind == FirewallRuleKind.GROUP.value:
            if sizes and spec.ref_group_id in sizes:
                total += sizes[spec.ref_group_id]
            else:
                total += _group_size(db, spec.ref_group_id)
        else:
            total += 1
    return total


def _check_rendered(db: Session, service_id: int, specs: Sequence[v.RuleSpec], limits: v.Limits, field_path: str, sizes=None) -> None:
    count = len(baseline_rules(db, load_context(db, service_id))) + _expanded(db, specs, sizes)
    if count > limits.max_rendered_rules:
        raise FirewallValidationError(
            f"this would put {count} rules on the server; at most {limits.max_rendered_rules} are allowed",
            code="rendered_limit_exceeded",
            field=field_path,
        )


def _validated_service_rules(
    db: Session, service: Service, actor: Actor, raw: Any, field_path: str = "rules"
) -> list[v.RuleSpec]:
    limits = get_limits(db)
    specs = v.validate_rules(
        raw,
        field_path,
        max_count=limits.max_rules_per_vm,
        allow_logging=actor.is_staff or limits.client_logging,
        max_group_refs=limits.max_groups_per_vm,
    )
    _check_references(db, specs, service.owner_user_id, actor, field_path)
    _check_rendered(db, service.id, specs, limits, field_path)
    return specs


def _apply_spec(row: FirewallRule, spec: v.RuleSpec) -> None:
    for key, value in spec.as_dict().items():
        setattr(row, key, value)


def _write_rules(
    db: Session,
    owner: dict[str, int],
    existing: Sequence[FirewallRule],
    specs: Sequence[v.RuleSpec],
    ids: Sequence[Optional[int]],
    actor: Actor,
) -> list[FirewallRule]:
    """Replace an ordered list. An entry carrying the ``id`` of an existing rule keeps that row (and
    so its ``rf:`` marker on Proxmox); other entries are new rows."""
    by_id = {rule.id: rule for rule in existing}
    keep: set[int] = set()
    out: list[FirewallRule] = []
    for position, (spec, rule_id) in enumerate(zip(specs, ids)):
        row = by_id.get(rule_id) if rule_id is not None and rule_id not in keep else None
        if row is None:
            row = FirewallRule(**owner, created_by_user_id=actor.user_id if actor.kind != Actor.BILLING else None)
            db.add(row)
        else:
            keep.add(row.id)
        _apply_spec(row, spec)
        row.position = position
        out.append(row)
    for rule in existing:
        if rule.id not in keep:
            db.delete(rule)
    db.flush()
    return out


def _raw_ids(raw: Any) -> list[Optional[int]]:
    out = []
    for item in raw or []:
        rule_id = item.get("id") if isinstance(item, Mapping) else None
        out.append(rule_id if isinstance(rule_id, int) and not isinstance(rule_id, bool) else None)
    return out


# Service-level changes


def set_firewall(
    db: Session,
    service_id: int,
    actor: Actor,
    *,
    revision: int,
    client_enabled: Optional[bool] = None,
    policy_in: Optional[str] = None,
    policy_out: Optional[str] = None,
    log_level_in: Optional[str] = None,
    log_level_out: Optional[str] = None,
    vm_config: Optional[Mapping[str, Any]] = None,
) -> ServiceFirewall:
    """Turn the client firewall on or off and set its policies and log levels."""
    service, row = _locked(db, service_id, actor, revision)
    limits = get_limits(db)
    before = _settings_snapshot(row)
    changes: dict[str, Any] = {}
    if policy_in is not None:
        changes["policy_in"] = v.validate_policy(policy_in, "policy_in")
    if policy_out is not None:
        changes["policy_out"] = v.validate_policy(policy_out, "policy_out")
    for key, value in (("log_level_in", log_level_in), ("log_level_out", log_level_out)):
        if value is None:
            continue
        level = v.validate_log_level(value, key)
        if level != FirewallLogLevel.NOLOG.value and not (actor.is_staff or limits.client_logging):
            raise FirewallValidationError("firewall logging is not available", code="logging_not_allowed", field=key)
        changes[key] = level
    if client_enabled is not None:
        if not isinstance(client_enabled, bool):
            raise FirewallValidationError("must be true or false", field="client_enabled")
        if client_enabled and not row.client_enabled:
            result = eligibility(db, service_id, vm_config=vm_config)
            if not result.eligible:
                raise FirewallNotEligible(result.reasons)
        changes["client_enabled"] = client_enabled
    changed = {key: value for key, value in changes.items() if _value(getattr(row, key)) != value}
    if not changed:
        return row
    for key, value in changed.items():
        setattr(row, key, value)
    if "client_enabled" in changed and row.state in (FirewallState.OFF, FirewallState.APPLIED):
        row.state = FirewallState.PENDING
    _bump(row)
    db.flush()
    after = _settings_snapshot(row)
    if "client_enabled" in changed:
        _audit(
            db,
            actor,
            "firewall.enabled" if row.client_enabled else "firewall.disabled",
            resource_type="service",
            resource_id=service_id,
            subject_user_id=service.owner_user_id,
            metadata={"before": before, "after": after},
        )
    if set(changed) - {"client_enabled"}:
        _audit(
            db,
            actor,
            "firewall.policy_changed",
            resource_type="service",
            resource_id=service_id,
            subject_user_id=service.owner_user_id,
            metadata={"before": before, "after": after},
        )
    return row


def _settings_snapshot(row: ServiceFirewall) -> dict[str, Any]:
    return {
        key: _value(getattr(row, key))
        for key in ("client_enabled", "policy_in", "policy_out", "log_level_in", "log_level_out")
    }


def set_admin_fields(
    db: Session,
    service_id: int,
    actor: Actor,
    *,
    revision: int,
    managed: Optional[bool] = None,
    excluded: Optional[bool] = None,
    enforce_baseline: Optional[bool] = None,
    admin_overrides: Any = UNSET,
) -> ServiceFirewall:
    """Staff only: ``managed``, ``excluded``, ``enforce_baseline`` and the per-service
    ``admin_overrides`` (for example ``{"smtp_out": "allow"}`` to unblock SMTP)."""
    _require_staff(actor)
    service, row = _locked(db, service_id, actor, revision)
    flags = {"managed": managed, "excluded": excluded, "enforce_baseline": enforce_baseline}
    managed_before = {key: bool(getattr(row, key)) for key in flags}
    overrides_before = dict(row.admin_overrides or {})
    for key, value in flags.items():
        if value is not None and not isinstance(value, bool):
            raise FirewallValidationError("must be true or false", field=key)
    for key, value in flags.items():
        if value is not None:
            setattr(row, key, value)
    if admin_overrides is not UNSET:
        row.admin_overrides = v.validate_admin_overrides(admin_overrides) or None
    managed_after = {key: bool(getattr(row, key)) for key in flags}
    overrides_after = dict(row.admin_overrides or {})
    if managed_after == managed_before and overrides_after == overrides_before:
        return row
    _bump(row)
    db.flush()
    if managed_after != managed_before:
        _audit(db, actor, "firewall.managed_changed", resource_type="service", resource_id=service_id,
               subject_user_id=service.owner_user_id, metadata={"before": managed_before, "after": managed_after})
    if overrides_after != overrides_before:
        _audit(db, actor, "firewall.admin_override_changed", resource_type="service", resource_id=service_id,
               subject_user_id=service.owner_user_id, metadata={"before": overrides_before, "after": overrides_after})
    return row


def replace_rules(db: Session, service_id: int, actor: Actor, rules: Any, *, revision: int) -> list[FirewallRule]:
    """Replace the whole ordered client list (group references included)."""
    service, row = _locked(db, service_id, actor, revision)
    specs = _validated_service_rules(db, service, actor, rules)
    existing = service_rules(db, service_id)
    before = _rule_summary(v.specs_from_rows(existing))
    written = _write_rules(db, {"service_id": service_id}, existing, specs, _raw_ids(rules), actor)
    _bump(row)
    db.flush()
    _audit(db, actor, "firewall.rules_replaced", resource_type="service", resource_id=service_id,
           subject_user_id=service.owner_user_id, metadata={"before": before, "after": _rule_summary(specs)})
    return written


def _insert(
    db: Session, service: Service, row: ServiceFirewall, actor: Actor, raw: Mapping[str, Any], position: Optional[int], field_path: str
) -> FirewallRule:
    existing = service_rules(db, service.id)
    raws: list[Any] = [_row_input(rule) for rule in existing]
    index = len(raws) if position is None else position
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= len(raws):
        raise FirewallValidationError(f"must be from 0 to {len(raws)}", field="position")
    raws.insert(index, raw)
    specs = _validated_service_rules(db, service, actor, raws, "rules")
    ids = [rule.id for rule in existing]
    ids.insert(index, None)
    written = _write_rules(db, {"service_id": service.id}, existing, specs, ids, actor)
    return written[index]


def _row_input(rule: FirewallRule) -> dict[str, Any]:
    data = v.specs_from_rows([rule])[0].as_dict()
    data["group_id"] = data.pop("ref_group_id")
    return {key: value for key, value in data.items() if value is not None}


def _reraise_at(exc: FirewallValidationError, index: int, field_path: str) -> None:
    prefix = f"rules[{index}]"
    if exc.field and exc.field.startswith(prefix):
        exc.field = field_path + exc.field[len(prefix):]
    raise exc


def add_rule(
    db: Session, service_id: int, actor: Actor, rule: Mapping[str, Any], *, revision: int, position: Optional[int] = None
) -> FirewallRule:
    """Insert one rule (appended unless ``position`` is given)."""
    service, row = _locked(db, service_id, actor, revision)
    if isinstance(rule, Mapping) and rule.get("kind") == FirewallRuleKind.GROUP.value:
        raise FirewallValidationError("use attach_group for group references", field="rule.kind")
    index = len(service_rules(db, service_id)) if position is None else position
    try:
        created = _insert(db, service, row, actor, rule, position, "rule")
    except FirewallValidationError as exc:
        _reraise_at(exc, index if isinstance(index, int) else 0, "rule")
    _bump(row)
    db.flush()
    _audit(db, actor, "firewall.rule_created", resource_type="service", resource_id=service_id,
           subject_user_id=service.owner_user_id,
           metadata={"rule_id": created.id, "position": created.position, "after": _rule_summary(v.specs_from_rows([created]))[0]})
    return created


def _service_rule(db: Session, service_id: int, rule_id: int) -> FirewallRule:
    rule = db.get(FirewallRule, rule_id)
    if rule is None or rule.service_id != service_id:
        raise FirewallNotFound("Rule not found", code="rule_not_found")
    return rule


def update_rule(
    db: Session, service_id: int, actor: Actor, rule_id: int, rule: Mapping[str, Any], *, revision: int
) -> FirewallRule:
    """Replace the fields of one rule; its position and id stay."""
    service, row = _locked(db, service_id, actor, revision)
    target = _service_rule(db, service_id, rule_id)
    existing = service_rules(db, service_id)
    index = next(i for i, r in enumerate(existing) if r.id == rule_id)
    if _value(target.kind) == FirewallRuleKind.GROUP.value or (
        isinstance(rule, Mapping) and rule.get("kind") == FirewallRuleKind.GROUP.value
    ):
        raise FirewallValidationError("group references are attached and detached, not edited", field="rule.kind")
    before = _rule_summary(v.specs_from_rows([target]))[0]
    raws = [_row_input(r) for r in existing]
    raws[index] = rule
    try:
        specs = _validated_service_rules(db, service, actor, raws)
    except FirewallValidationError as exc:
        _reraise_at(exc, index, "rule")
    _apply_spec(target, specs[index])
    _bump(row)
    db.flush()
    after = _rule_summary([specs[index]])[0]
    if after != before:
        _audit(db, actor, "firewall.rule_updated", resource_type="service", resource_id=service_id,
               subject_user_id=service.owner_user_id, metadata={"rule_id": rule_id, "before": before, "after": after})
    return target


def delete_rule(db: Session, service_id: int, actor: Actor, rule_id: int, *, revision: int) -> None:
    service, row = _locked(db, service_id, actor, revision)
    target = _service_rule(db, service_id, rule_id)
    before = _rule_summary(v.specs_from_rows([target]))[0]
    db.delete(target)
    db.flush()
    _renumber(db, service_id)
    _bump(row)
    db.flush()
    _audit(db, actor, "firewall.rule_deleted", resource_type="service", resource_id=service_id,
           subject_user_id=service.owner_user_id, metadata={"rule_id": rule_id, "before": before})


def _renumber(db: Session, service_id: int) -> None:
    for position, rule in enumerate(service_rules(db, service_id)):
        rule.position = position


def reorder_rules(db: Session, service_id: int, actor: Actor, ids: Sequence[int], *, revision: int) -> list[FirewallRule]:
    """``ids`` must list every rule of the VM exactly once, in the new order."""
    service, row = _locked(db, service_id, actor, revision)
    wanted = v.unique_ids(ids, "ids")
    existing = service_rules(db, service_id)
    if sorted(wanted) != sorted(rule.id for rule in existing):
        raise FirewallValidationError("must list every rule of this server exactly once", code="ids_mismatch", field="ids")
    before = [rule.id for rule in existing]
    if before == wanted:
        return existing
    by_id = {rule.id: rule for rule in existing}
    for position, rule_id in enumerate(wanted):
        by_id[rule_id].position = position
    _bump(row)
    db.flush()
    _audit(db, actor, "firewall.rules_reordered", resource_type="service", resource_id=service_id,
           subject_user_id=service.owner_user_id, metadata={"before": before, "after": wanted})
    return [by_id[rule_id] for rule_id in wanted]


def apply_preset(db: Session, service_id: int, actor: Actor, preset_key: str, *, revision: int) -> ServiceFirewall:
    """Append the preset's rules that the VM does not already have; ``allow_all`` sets
    ``policy_in`` ACCEPT and adds nothing."""
    preset = get_preset(preset_key)
    service, row = _locked(db, service_id, actor, revision)
    existing = service_rules(db, service_id)
    have = v.specs_from_rows(existing)
    raws: list[Any] = [_row_input(rule) for rule in existing]
    added = []
    for rule in preset.rules:
        spec = v.validate_rule(rule)
        if any(spec.same_effect(other) for other in have):
            continue
        raws.append(dict(rule))
        added.append(spec)
    policy_before = _value(row.policy_in)
    if preset.policy_in is not None:
        row.policy_in = preset.policy_in
    if not added and _value(row.policy_in) == policy_before:
        return row
    if added:
        specs = _validated_service_rules(db, service, actor, raws)
        _write_rules(db, {"service_id": service_id}, existing, specs, [r.id for r in existing] + [None] * len(added), actor)
    _bump(row)
    db.flush()
    _audit(db, actor, "firewall.preset_applied", resource_type="service", resource_id=service_id,
           subject_user_id=service.owner_user_id,
           metadata={"preset": preset.key, "added": _rule_summary(added),
                     "policy_in": {"before": policy_before, "after": _value(row.policy_in)}})
    return row


def attach_group(
    db: Session, service_id: int, actor: Actor, group_id: int, *, revision: int, position: Optional[int] = None
) -> FirewallRule:
    service, row = _locked(db, service_id, actor, revision)
    index = len(service_rules(db, service_id)) if position is None else position
    try:
        created = _insert(db, service, row, actor, {"kind": "group", "group_id": group_id}, position, "group_id")
    except FirewallValidationError as exc:
        prefix = f"rules[{index}]"
        if exc.field and exc.field.startswith(prefix):
            exc.field = "group_id" if exc.field.endswith(".group_id") else exc.field[len(prefix):].lstrip(".") or "group_id"
        raise
    _bump(row)
    db.flush()
    _audit(db, actor, "firewall.group_attached", resource_type="service", resource_id=service_id,
           subject_user_id=service.owner_user_id, metadata={"group_id": group_id, "position": created.position})
    return created


def detach_group(db: Session, service_id: int, actor: Actor, group_id: int, *, revision: int) -> None:
    service, row = _locked(db, service_id, actor, revision)
    refs = [rule for rule in service_rules(db, service_id) if rule.ref_group_id == group_id]
    if not refs:
        raise FirewallNotFound("This group is not attached", code="group_not_attached")
    for rule in refs:
        db.delete(rule)
    db.flush()
    _renumber(db, service_id)
    _bump(row)
    db.flush()
    _audit(db, actor, "firewall.group_detached", resource_type="service", resource_id=service_id,
           subject_user_id=service.owner_user_id, metadata={"group_id": group_id})


def remove_foreign_references(db: Session, service_id: int, actor: Actor) -> list[int]:
    """Remove rules referencing groups or IP sets the service's current owner may not use (after an
    ownership change). Returns the removed rule ids."""
    _require_staff(actor)
    service = _service(db, service_id)
    row = db.get(ServiceFirewall, service_id)
    if row is None:
        return []
    removed = []
    for rule in service_rules(db, service_id):
        for model, ref in ((FirewallGroup, rule.ref_group_id), (FirewallIpset, rule.remote_ipset_id)):
            if ref is None:
                continue
            obj = db.get(model, ref)
            try:
                v.check_reference(obj, service.owner_user_id, actor_is_staff=False, field_path="")
            except FirewallValidationError:
                if _value(obj.scope) == FirewallObjectScope.CLIENT.value:
                    removed.append(rule.id)
                    db.delete(rule)
                    break
    if not removed:
        return []
    db.flush()
    _renumber(db, service_id)
    _bump(row)
    db.flush()
    _audit(db, actor, "firewall.references_removed", resource_type="service", resource_id=service_id,
           subject_user_id=service.owner_user_id, metadata={"rule_ids": removed})
    return removed


# Which VMs render an object


def _all_firewall_ids(db: Session) -> set[int]:
    return set(db.execute(select(ServiceFirewall.service_id)).scalars())


def _services_of_baseline(db: Session, baseline: FirewallBaseline) -> set[int]:
    scope = _value(baseline.scope_type)
    if scope == FirewallBaselineScope.GLOBAL.value:
        return _all_firewall_ids(db)
    if scope == FirewallBaselineScope.LOCATION.value:
        query = (
            select(ServiceFirewall.service_id)
            .join(ServiceVm, ServiceVm.service_id == ServiceFirewall.service_id)
            .join(ProxmoxCluster, ProxmoxCluster.id == ServiceVm.proxmox_cluster_id)
            .where(ProxmoxCluster.location_id == baseline.scope_id)
        )
    else:
        query = (
            select(ServiceFirewall.service_id)
            .join(ServiceBilling, ServiceBilling.service_id == ServiceFirewall.service_id)
            .where(ServiceBilling.product_id == baseline.scope_id)
        )
    return set(db.execute(query).scalars())


def _services_of_baselines(db: Session, baseline_ids: Iterable[int]) -> set[int]:
    out: set[int] = set()
    for baseline_id in set(baseline_ids):
        baseline = db.get(FirewallBaseline, baseline_id)
        if baseline is not None:
            out |= _services_of_baseline(db, baseline)
    return out


def _services_of_groups(db: Session, group_ids: Iterable[int]) -> set[int]:
    ids = list(set(group_ids))
    if not ids:
        return set()
    rows = db.execute(
        select(FirewallRule.service_id, FirewallRule.baseline_id).where(FirewallRule.ref_group_id.in_(ids))
    ).all()
    out = {service_id for service_id, _ in rows if service_id is not None}
    return out | _services_of_baselines(db, (b for _, b in rows if b is not None))


def _services_of_ipset(db: Session, ipset: FirewallIpset) -> set[int]:
    rows = db.execute(
        select(FirewallRule.service_id, FirewallRule.group_id, FirewallRule.baseline_id).where(
            FirewallRule.remote_ipset_id == ipset.id
        )
    ).all()
    out = {s for s, _, _ in rows if s is not None}
    out |= _services_of_groups(db, (g for _, g, _ in rows if g is not None))
    out |= _services_of_baselines(db, (b for _, _, b in rows if b is not None))
    management = _value(ipset.purpose) == FirewallIpsetPurpose.MANAGEMENT.value
    if not management:
        for baseline in db.execute(select(FirewallBaseline)).scalars():
            if ipset.id in ((baseline.settings or {}).get("management_ipset_ids") or []):
                management = True
                break
    if management and _value(ipset.scope) == FirewallObjectScope.ADMIN.value:
        out |= _all_firewall_ids(db)
    return out


def bump_revisions(db: Session, service_ids: Iterable[int]) -> int:
    """One UPDATE over every affected VM; returns how many rows changed."""
    ids = sorted(set(service_ids))
    if not ids:
        return 0
    result = db.execute(
        update(ServiceFirewall)
        .where(ServiceFirewall.service_id.in_(ids))
        .values(revision=ServiceFirewall.revision + 1)
        .execution_options(synchronize_session="fetch")
    )
    return int(result.rowcount or 0)


# Security groups


def _owner_for_new(actor: Actor, owner_user_id: Optional[int]) -> tuple[str, Optional[int]]:
    """(scope, owner) of a new group or IP set."""
    if actor.is_staff:
        if owner_user_id is None:
            return FirewallObjectScope.ADMIN.value, None
        return FirewallObjectScope.CLIENT.value, owner_user_id
    if actor.user_id is None or owner_user_id not in (None, actor.user_id):
        raise FirewallForbidden("You can create objects only in your own account", code="not_owner", field="owner_user_id")
    return FirewallObjectScope.CLIENT.value, actor.user_id


def _check_object_access(obj: Any, actor: Actor, what: str) -> None:
    if _value(obj.scope) == FirewallObjectScope.ADMIN.value:
        _require_staff(actor)
        return
    if actor.is_staff:
        return
    if actor.user_id is None or obj.owner_user_id != actor.user_id:
        raise FirewallNotFound(f"{what} not found", code="not_found")


def _check_revision(obj: Any, revision: Optional[int]) -> None:
    if revision is not None and int(revision) != obj.revision:
        raise FirewallChanged(obj.revision)


def _unique_name(db: Session, model, owner_user_id: Optional[int], name: str, exclude_id: Optional[int] = None) -> None:
    query = select(model.id).where(model.name == name)
    query = query.where(model.owner_user_id.is_(None) if owner_user_id is None else model.owner_user_id == owner_user_id)
    if exclude_id is not None:
        query = query.where(model.id != exclude_id)
    if db.execute(query).first() is not None:
        raise FirewallConflict("This name is already used", code="duplicate_name", field="name")


def _count_owned(db: Session, model, owner_user_id: Optional[int]) -> int:
    return int(db.execute(select(func.count()).select_from(model).where(model.owner_user_id == owner_user_id)).scalar_one())


def _validated_group_rules(db: Session, actor: Actor, raw: Any, owner_user_id: Optional[int]) -> list[v.RuleSpec]:
    limits = get_limits(db)
    specs = v.validate_rules(
        raw,
        "rules",
        max_count=limits.max_rules_per_group,
        allow_group=False,
        allow_logging=actor.is_staff or limits.client_logging,
    )
    _check_references(db, specs, owner_user_id, actor, "rules", is_group=True)
    return specs


def group_rules(db: Session, group_id: int) -> list[FirewallRule]:
    return list(
        db.execute(
            select(FirewallRule).where(FirewallRule.group_id == group_id).order_by(FirewallRule.position, FirewallRule.id)
        ).scalars()
    )


def list_groups(db: Session, actor: Actor, *, owner_user_id: Optional[int] = None) -> list[FirewallGroup]:
    """A client sees its own groups plus admin groups marked ``client_selectable``; staff see all
    (or one owner's with ``owner_user_id``)."""
    query = select(FirewallGroup).order_by(FirewallGroup.name, FirewallGroup.id)
    if not actor.is_staff:
        query = query.where(
            or_(
                FirewallGroup.owner_user_id == actor.user_id,
                (FirewallGroup.scope == FirewallObjectScope.ADMIN) & FirewallGroup.client_selectable.is_(True),
            )
        )
    elif owner_user_id is not None:
        query = query.where(FirewallGroup.owner_user_id == owner_user_id)
    return list(db.execute(query).scalars())


def get_group(db: Session, actor: Actor, group_id: int) -> FirewallGroup:
    group = db.get(FirewallGroup, group_id)
    if group is None:
        raise FirewallNotFound("Security group not found", code="not_found")
    if not actor.is_staff and _value(group.scope) == FirewallObjectScope.ADMIN.value and group.client_selectable:
        return group
    _check_object_access(group, actor, "Security group")
    return group


def create_group(
    db: Session,
    actor: Actor,
    *,
    name: str,
    description: Optional[str] = None,
    rules: Any = (),
    owner_user_id: Optional[int] = None,
    client_selectable: bool = False,
) -> FirewallGroup:
    scope, owner = _owner_for_new(actor, owner_user_id)
    name = v.validate_name(name)
    if client_selectable and scope != FirewallObjectScope.ADMIN.value:
        raise FirewallValidationError("only admin groups can be offered to clients", field="client_selectable")
    if owner is not None:
        limit = get_limits(db).max_groups_per_client
        if _count_owned(db, FirewallGroup, owner) >= limit:
            raise FirewallValidationError(f"at most {limit} security groups are allowed", code="limit_exceeded", field="name")
    _unique_name(db, FirewallGroup, owner, name)
    specs = _validated_group_rules(db, actor, list(rules or []), owner)
    group = FirewallGroup(
        name=name,
        description=v.validate_description(description),
        scope=scope,
        owner_user_id=owner,
        client_selectable=bool(client_selectable),
        revision=1,
    )
    db.add(group)
    db.flush()
    _write_rules(db, {"group_id": group.id}, [], specs, [None] * len(specs), actor)
    _audit(db, actor, "firewall.group_created", resource_type="firewall_group", resource_id=group.id,
           subject_user_id=owner, metadata={"after": {"name": name, "rules": _rule_summary(specs)}})
    return group


def update_group(
    db: Session,
    actor: Actor,
    group_id: int,
    *,
    revision: Optional[int] = None,
    name: Any = UNSET,
    description: Any = UNSET,
    rules: Any = UNSET,
    client_selectable: Any = UNSET,
) -> FirewallGroup:
    """Rename or describe a group, or replace its rules (``rules``); bumps every VM rendering it."""
    group = db.get(FirewallGroup, group_id)
    if group is None:
        raise FirewallNotFound("Security group not found", code="not_found")
    _check_object_access(group, actor, "Security group")
    _check_revision(group, revision)
    existing = group_rules(db, group_id)
    before = {"name": group.name, "description": group.description, "client_selectable": bool(group.client_selectable),
              "rules": _rule_summary(v.specs_from_rows(existing))}
    if name is not UNSET:
        new_name = v.validate_name(name)
        _unique_name(db, FirewallGroup, group.owner_user_id, new_name, exclude_id=group.id)
        group.name = new_name
    if description is not UNSET:
        group.description = v.validate_description(description)
    if client_selectable is not UNSET:
        if _value(group.scope) != FirewallObjectScope.ADMIN.value and client_selectable:
            raise FirewallValidationError("only admin groups can be offered to clients", field="client_selectable")
        group.client_selectable = bool(client_selectable)
    affected: set[int] = set()
    if rules is not UNSET:
        specs = _validated_group_rules(db, actor, list(rules or []), group.owner_user_id)
        affected = _services_of_groups(db, [group_id])
        limits = get_limits(db)
        size = sum(1 for spec in specs if spec.kind == FirewallRuleKind.RULE.value)
        for service_id in sorted(affected):
            current = v.specs_from_rows(service_rules(db, service_id))
            if not any(spec.ref_group_id == group_id for spec in current):
                continue
            _check_rendered(db, service_id, current, limits, "rules", sizes={group_id: size})
        _write_rules(db, {"group_id": group_id}, existing, specs, _raw_ids(rules), actor)
    after = {"name": group.name, "description": group.description, "client_selectable": bool(group.client_selectable),
             "rules": _rule_summary(v.specs_from_rows(group_rules(db, group_id)))}
    if after == before:
        return group
    group.revision = (group.revision or 0) + 1
    if after["rules"] != before["rules"]:
        bump_revisions(db, affected)
    db.flush()
    _audit(db, actor, "firewall.group_updated", resource_type="firewall_group", resource_id=group.id,
           subject_user_id=group.owner_user_id, metadata={"before": before, "after": after})
    return group


def delete_group(db: Session, actor: Actor, group_id: int, *, revision: Optional[int] = None, detach: bool = False) -> None:
    """Refused while a VM or baseline references the group, unless ``detach`` removes those references."""
    group = db.get(FirewallGroup, group_id)
    if group is None:
        raise FirewallNotFound("Security group not found", code="not_found")
    _check_object_access(group, actor, "Security group")
    _check_revision(group, revision)
    refs = list(db.execute(select(FirewallRule).where(FirewallRule.ref_group_id == group_id)).scalars())
    if refs and not detach:
        raise FirewallConflict("This security group is in use", code="in_use")
    affected = _services_of_groups(db, [group_id])
    touched_services = {rule.service_id for rule in refs if rule.service_id is not None}
    touched_baselines = {rule.baseline_id for rule in refs if rule.baseline_id is not None}
    for rule in refs:
        db.delete(rule)
    db.flush()
    for service_id in touched_services:
        _renumber(db, service_id)
    for baseline_id in touched_baselines:
        for position, rule in enumerate(_baseline_rules(db, baseline_id)):
            rule.position = position
    for baseline_id in touched_baselines:
        baseline = db.get(FirewallBaseline, baseline_id)
        baseline.revision = (baseline.revision or 0) + 1
    name = group.name
    db.delete(group)
    bump_revisions(db, affected)
    db.flush()
    _audit(db, actor, "firewall.group_deleted", resource_type="firewall_group", resource_id=group_id,
           subject_user_id=group.owner_user_id,
           metadata={"before": {"name": name}, "detached_from_services": sorted(touched_services)})


# IP sets


def ipset_entries(db: Session, ipset_id: int) -> list[FirewallIpsetEntry]:
    return list(
        db.execute(
            select(FirewallIpsetEntry).where(FirewallIpsetEntry.ipset_id == ipset_id).order_by(FirewallIpsetEntry.id)
        ).scalars()
    )


def list_ipsets(db: Session, actor: Actor, *, owner_user_id: Optional[int] = None) -> list[FirewallIpset]:
    query = select(FirewallIpset).order_by(FirewallIpset.name, FirewallIpset.id)
    if not actor.is_staff:
        query = query.where(
            or_(
                FirewallIpset.owner_user_id == actor.user_id,
                (FirewallIpset.scope == FirewallObjectScope.ADMIN) & FirewallIpset.client_selectable.is_(True),
            )
        )
    elif owner_user_id is not None:
        query = query.where(FirewallIpset.owner_user_id == owner_user_id)
    return list(db.execute(query).scalars())


def get_ipset(db: Session, actor: Actor, ipset_id: int) -> FirewallIpset:
    ipset = db.get(FirewallIpset, ipset_id)
    if ipset is None:
        raise FirewallNotFound("IP set not found", code="not_found")
    if not actor.is_staff and _value(ipset.scope) == FirewallObjectScope.ADMIN.value and ipset.client_selectable:
        return ipset
    _check_object_access(ipset, actor, "IP set")
    return ipset


def _write_entries(db: Session, ipset: FirewallIpset, specs: Sequence[v.IpsetEntrySpec]) -> None:
    existing = {entry.cidr: entry for entry in ipset_entries(db, ipset.id)}
    wanted = {spec.cidr for spec in specs}
    for cidr, entry in existing.items():
        if cidr not in wanted:
            db.delete(entry)
    db.flush()
    for spec in specs:
        entry = existing.get(spec.cidr)
        if entry is None:
            db.add(FirewallIpsetEntry(ipset_id=ipset.id, cidr=spec.cidr, nomatch=spec.nomatch, comment=spec.comment))
        else:
            entry.nomatch = spec.nomatch
            entry.comment = spec.comment
    db.flush()
    db.expire(ipset, ["entries"])


def _entries_summary(db: Session, ipset_id: int) -> list[dict[str, Any]]:
    return [{"cidr": e.cidr, "nomatch": bool(e.nomatch), "comment": e.comment} for e in ipset_entries(db, ipset_id)]


def create_ipset(
    db: Session,
    actor: Actor,
    *,
    name: str,
    entries: Any = (),
    description: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    client_selectable: bool = False,
    purpose: str = FirewallIpsetPurpose.GENERAL.value,
) -> FirewallIpset:
    scope, owner = _owner_for_new(actor, owner_user_id)
    name = v.validate_name(name)
    if purpose not in [p.value for p in FirewallIpsetPurpose]:
        raise FirewallValidationError("must be general or management", field="purpose")
    if scope != FirewallObjectScope.ADMIN.value and (client_selectable or purpose != FirewallIpsetPurpose.GENERAL.value):
        raise FirewallForbidden("Only staff can change this", code="admin_only",
                                field="client_selectable" if client_selectable else "purpose")
    limits = get_limits(db)
    if owner is not None and _count_owned(db, FirewallIpset, owner) >= limits.max_ipsets_per_client:
        raise FirewallValidationError(
            f"at most {limits.max_ipsets_per_client} IP sets are allowed", code="limit_exceeded", field="name"
        )
    _unique_name(db, FirewallIpset, owner, name)
    specs = v.validate_ipset_entries(list(entries or []), max_entries=limits.max_ipset_entries)
    ipset = FirewallIpset(
        name=name,
        description=v.validate_description(description),
        scope=scope,
        owner_user_id=owner,
        client_selectable=bool(client_selectable),
        purpose=purpose,
        revision=1,
    )
    db.add(ipset)
    db.flush()
    _write_entries(db, ipset, specs)
    affected = _services_of_ipset(db, ipset) if purpose == FirewallIpsetPurpose.MANAGEMENT.value else set()
    bump_revisions(db, affected)
    _audit(db, actor, "firewall.ipset_created", resource_type="firewall_ipset", resource_id=ipset.id,
           subject_user_id=owner, metadata={"after": {"name": name, "purpose": purpose, "entries": _entries_summary(db, ipset.id)}})
    return ipset


def update_ipset(
    db: Session,
    actor: Actor,
    ipset_id: int,
    *,
    revision: Optional[int] = None,
    name: Any = UNSET,
    description: Any = UNSET,
    entries: Any = UNSET,
    client_selectable: Any = UNSET,
    purpose: Any = UNSET,
) -> FirewallIpset:
    ipset = db.get(FirewallIpset, ipset_id)
    if ipset is None:
        raise FirewallNotFound("IP set not found", code="not_found")
    _check_object_access(ipset, actor, "IP set")
    _check_revision(ipset, revision)
    admin = _value(ipset.scope) == FirewallObjectScope.ADMIN.value
    affected_before = _services_of_ipset(db, ipset)
    before = {"name": ipset.name, "description": ipset.description, "purpose": _value(ipset.purpose),
              "client_selectable": bool(ipset.client_selectable), "entries": _entries_summary(db, ipset_id)}
    if name is not UNSET:
        new_name = v.validate_name(name)
        _unique_name(db, FirewallIpset, ipset.owner_user_id, new_name, exclude_id=ipset.id)
        ipset.name = new_name
    if description is not UNSET:
        ipset.description = v.validate_description(description)
    if client_selectable is not UNSET:
        if not admin and client_selectable:
            raise FirewallForbidden("Only staff can change this", code="admin_only", field="client_selectable")
        ipset.client_selectable = bool(client_selectable)
    if purpose is not UNSET:
        if purpose not in [p.value for p in FirewallIpsetPurpose]:
            raise FirewallValidationError("must be general or management", field="purpose")
        if not admin and purpose != FirewallIpsetPurpose.GENERAL.value:
            raise FirewallForbidden("Only staff can change this", code="admin_only", field="purpose")
        ipset.purpose = purpose
    if entries is not UNSET:
        specs = v.validate_ipset_entries(list(entries or []), max_entries=get_limits(db).max_ipset_entries)
        _write_entries(db, ipset, specs)
    db.flush()
    after = {"name": ipset.name, "description": ipset.description, "purpose": _value(ipset.purpose),
             "client_selectable": bool(ipset.client_selectable), "entries": _entries_summary(db, ipset_id)}
    if after == before:
        return ipset
    ipset.revision = (ipset.revision or 0) + 1
    if after["entries"] != before["entries"] or after["purpose"] != before["purpose"]:
        bump_revisions(db, affected_before | _services_of_ipset(db, ipset))
    db.flush()
    _audit(db, actor, "firewall.ipset_updated", resource_type="firewall_ipset", resource_id=ipset.id,
           subject_user_id=ipset.owner_user_id, metadata={"before": before, "after": after})
    return ipset


def delete_ipset(db: Session, actor: Actor, ipset_id: int, *, revision: Optional[int] = None) -> None:
    """Refused while a rule references the IP set."""
    ipset = db.get(FirewallIpset, ipset_id)
    if ipset is None:
        raise FirewallNotFound("IP set not found", code="not_found")
    _check_object_access(ipset, actor, "IP set")
    _check_revision(ipset, revision)
    in_use = db.execute(select(FirewallRule.id).where(FirewallRule.remote_ipset_id == ipset_id)).first()
    if in_use is not None:
        raise FirewallConflict("This IP set is in use", code="in_use")
    affected = _services_of_ipset(db, ipset)
    for baseline in db.execute(select(FirewallBaseline)).scalars():
        ids = (baseline.settings or {}).get("management_ipset_ids")
        if ids and ipset_id in ids:
            baseline.settings = {**baseline.settings, "management_ipset_ids": [i for i in ids if i != ipset_id]}
            baseline.revision = (baseline.revision or 0) + 1
    before = {"name": ipset.name, "entries": _entries_summary(db, ipset_id)}
    owner = ipset.owner_user_id
    db.delete(ipset)
    bump_revisions(db, affected)
    db.flush()
    _audit(db, actor, "firewall.ipset_deleted", resource_type="firewall_ipset", resource_id=ipset_id,
           subject_user_id=owner, metadata={"before": before})


# Baselines (staff only)


def _baseline_rules(db: Session, baseline_id: int) -> list[FirewallRule]:
    return list(
        db.execute(
            select(FirewallRule)
            .where(FirewallRule.baseline_id == baseline_id)
            .order_by(FirewallRule.position, FirewallRule.id)
        ).scalars()
    )


def _validated_baseline_rules(db: Session, actor: Actor, raw: Any) -> list[v.RuleSpec]:
    specs = v.validate_rules(raw, "rules", max_count=get_limits(db).max_rendered_rules)
    _check_references(db, specs, None, actor, "rules")
    return specs


def _check_management_ids(db: Session, settings: dict[str, Any]) -> None:
    for index, ipset_id in enumerate(settings.get("management_ipset_ids") or []):
        ipset = db.get(FirewallIpset, ipset_id)
        if ipset is None or _value(ipset.scope) != FirewallObjectScope.ADMIN.value:
            raise FirewallValidationError(
                "must be an admin IP set", code="reference_not_allowed", field=f"settings.management_ipset_ids[{index}]"
            )


def _baseline_snapshot(db: Session, baseline: FirewallBaseline) -> dict[str, Any]:
    return {
        "name": baseline.name,
        "enabled": bool(baseline.enabled),
        "settings": dict(baseline.settings or {}),
        "rules": _rule_summary(v.specs_from_rows(_baseline_rules(db, baseline.id))),
    }


def create_baseline(
    db: Session,
    actor: Actor,
    *,
    name: str,
    scope_type: str,
    scope_id: Optional[int] = None,
    settings: Any = None,
    rules: Any = (),
    enabled: bool = True,
) -> FirewallBaseline:
    _require_staff(actor)
    if scope_type not in [s.value for s in FirewallBaselineScope]:
        raise FirewallValidationError("must be global, location or product", field="scope_type")
    if (scope_type == FirewallBaselineScope.GLOBAL.value) != (scope_id is None):
        raise FirewallValidationError("a global baseline has no scope id; the others need one", field="scope_id")
    exists = db.execute(
        select(FirewallBaseline.id).where(
            FirewallBaseline.scope_type == scope_type,
            FirewallBaseline.scope_id.is_(None) if scope_id is None else FirewallBaseline.scope_id == scope_id,
        )
    ).first()
    if exists is not None:
        raise FirewallConflict("This scope already has a baseline", code="duplicate_baseline", field="scope_type")
    clean = v.validate_baseline_settings(settings)
    _check_management_ids(db, clean)
    specs = _validated_baseline_rules(db, actor, list(rules or []))
    baseline = FirewallBaseline(
        name=v._text(name, "name", 64, required=True),
        scope_type=scope_type,
        scope_id=scope_id,
        enabled=bool(enabled),
        settings=clean or None,
        revision=1,
    )
    db.add(baseline)
    db.flush()
    _write_rules(db, {"baseline_id": baseline.id}, [], specs, [None] * len(specs), actor)
    bump_revisions(db, _services_of_baseline(db, baseline))
    _audit(db, actor, "firewall.baseline_created", resource_type="firewall_baseline", resource_id=baseline.id,
           subject_user_id=None, metadata={"scope": {"type": scope_type, "id": scope_id}, "after": _baseline_snapshot(db, baseline)})
    return baseline


def update_baseline(
    db: Session,
    actor: Actor,
    baseline_id: int,
    *,
    revision: Optional[int] = None,
    name: Any = UNSET,
    settings: Any = UNSET,
    rules: Any = UNSET,
    enabled: Any = UNSET,
) -> FirewallBaseline:
    _require_staff(actor)
    baseline = db.get(FirewallBaseline, baseline_id)
    if baseline is None:
        raise FirewallNotFound("Baseline not found", code="not_found")
    _check_revision(baseline, revision)
    before = _baseline_snapshot(db, baseline)
    if name is not UNSET:
        baseline.name = v._text(name, "name", 64, required=True)
    if enabled is not UNSET:
        baseline.enabled = bool(enabled)
    if settings is not UNSET:
        clean = v.validate_baseline_settings(settings)
        _check_management_ids(db, clean)
        baseline.settings = clean or None
    if rules is not UNSET:
        specs = _validated_baseline_rules(db, actor, list(rules or []))
        _write_rules(db, {"baseline_id": baseline_id}, _baseline_rules(db, baseline_id), specs, _raw_ids(rules), actor)
    db.flush()
    after = _baseline_snapshot(db, baseline)
    if after == before:
        return baseline
    baseline.revision = (baseline.revision or 0) + 1
    bump_revisions(db, _services_of_baseline(db, baseline))
    db.flush()
    _audit(db, actor, "firewall.baseline_updated", resource_type="firewall_baseline", resource_id=baseline.id,
           subject_user_id=None, metadata={"before": before, "after": after})
    return baseline


def delete_baseline(db: Session, actor: Actor, baseline_id: int, *, revision: Optional[int] = None) -> None:
    _require_staff(actor)
    baseline = db.get(FirewallBaseline, baseline_id)
    if baseline is None:
        raise FirewallNotFound("Baseline not found", code="not_found")
    _check_revision(baseline, revision)
    before = _baseline_snapshot(db, baseline)
    affected = _services_of_baseline(db, baseline)
    db.delete(baseline)
    bump_revisions(db, affected)
    db.flush()
    _audit(db, actor, "firewall.baseline_deleted", resource_type="firewall_baseline", resource_id=baseline_id,
           subject_user_id=None, metadata={"before": before})


# Location settings (staff only, except readiness recorded by the system)


def get_location_settings(db: Session, location_id: int) -> FirewallLocationSettings:
    """The location's settings row, created (disabled) on first use."""
    row = db.get(FirewallLocationSettings, location_id)
    if row is None:
        row = FirewallLocationSettings(location_id=location_id, enabled=False, nic_flag_on_provision=True,
                                       enforce_baseline_on_all=False, smtp_block_new_vms=True,
                                       anti_spoof_new_vms=False)
        db.add(row)
        db.flush()
    return row


def _location_snapshot(row: FirewallLocationSettings) -> dict[str, Any]:
    return {
        "enabled": bool(row.enabled),
        "nic_flag_on_provision": bool(row.nic_flag_on_provision),
        "enforce_baseline_on_all": bool(row.enforce_baseline_on_all),
        "smtp_block_new_vms": bool(row.smtp_block_new_vms),
        "anti_spoof_new_vms": bool(row.anti_spoof_new_vms),
        "ipv6_allowed_prefixes": list(row.ipv6_allowed_prefixes or []),
    }


def _services_in_location(db: Session, location_id: int) -> set[int]:
    query = (
        select(ServiceFirewall.service_id)
        .join(ServiceVm, ServiceVm.service_id == ServiceFirewall.service_id)
        .join(ProxmoxCluster, ProxmoxCluster.id == ServiceVm.proxmox_cluster_id)
        .where(ProxmoxCluster.location_id == location_id)
    )
    return set(db.execute(query).scalars())


def update_location_settings(
    db: Session,
    actor: Actor,
    location_id: int,
    *,
    enabled: Optional[bool] = None,
    nic_flag_on_provision: Optional[bool] = None,
    enforce_baseline_on_all: Optional[bool] = None,
    smtp_block_new_vms: Optional[bool] = None,
    anti_spoof_new_vms: Optional[bool] = None,
    ipv6_allowed_prefixes: Any = UNSET,
    staging: bool = False,
) -> FirewallLocationSettings:
    """Turning a location on needs a passing readiness result for every cluster cached on the row,
    unless ``staging`` (enabled before the cluster step, not enforced)."""
    _require_staff(actor)
    row = get_location_settings(db, location_id)
    before = _location_snapshot(row)
    for key, value in (("enabled", enabled), ("nic_flag_on_provision", nic_flag_on_provision),
                       ("enforce_baseline_on_all", enforce_baseline_on_all),
                       ("smtp_block_new_vms", smtp_block_new_vms), ("anti_spoof_new_vms", anti_spoof_new_vms)):
        if value is not None and not isinstance(value, bool):
            raise FirewallValidationError("must be true or false", field=key)
    if enabled and not row.enabled and not staging:
        readiness = row.readiness or {}
        clusters = readiness.get("clusters") or {}
        if not clusters or not all(entry.get("ready") for entry in clusters.values()):
            raise FirewallConflict("The readiness check has not passed for every cluster here", code="not_ready",
                                   field="enabled")
    if enabled is not None:
        row.enabled = enabled
        if enabled and nic_flag_on_provision is None:
            row.nic_flag_on_provision = True
    if nic_flag_on_provision is not None:
        row.nic_flag_on_provision = nic_flag_on_provision
    if enforce_baseline_on_all is not None:
        row.enforce_baseline_on_all = enforce_baseline_on_all
    if smtp_block_new_vms is not None:
        row.smtp_block_new_vms = smtp_block_new_vms
    if anti_spoof_new_vms is not None:
        row.anti_spoof_new_vms = anti_spoof_new_vms
    if ipv6_allowed_prefixes is not UNSET:
        row.ipv6_allowed_prefixes = v.validate_prefixes(ipv6_allowed_prefixes)
    after = _location_snapshot(row)
    if after == before:
        return row
    # ``smtp_block_new_vms`` only affects VMs provisioned later, so it bumps nothing.
    rendered_keys = ("enabled", "enforce_baseline_on_all", "anti_spoof_new_vms", "ipv6_allowed_prefixes")
    if any(before[key] != after[key] for key in rendered_keys):
        bump_revisions(db, _services_in_location(db, location_id))
    db.flush()
    _audit(db, actor, "firewall.location_updated", resource_type="location", resource_id=location_id,
           subject_user_id=None, metadata={"before": before, "after": after, "staging": bool(staging)})
    return row


def record_readiness(db: Session, location_id: int, cluster_id: int, result: Mapping[str, Any], *, checked_at=None) -> FirewallLocationSettings:
    """Cache one cluster's readiness result (``Readiness.to_dict()``) on the location row."""
    from datetime import datetime, timezone

    row = get_location_settings(db, location_id)
    readiness = dict(row.readiness or {})
    clusters = dict(readiness.get("clusters") or {})
    clusters[str(cluster_id)] = dict(result)
    readiness["clusters"] = clusters
    row.readiness = readiness
    row.readiness_checked_at = checked_at or datetime.now(timezone.utc)
    db.flush()
    return row


__all__ = [
    "Actor",
    "Eligibility",
    "UNSET",
    "add_rule",
    "apply_preset",
    "attach_group",
    "bump_revisions",
    "create_baseline",
    "create_group",
    "create_ipset",
    "delete_baseline",
    "delete_group",
    "delete_ipset",
    "delete_rule",
    "detach_group",
    "eligibility",
    "get_firewall",
    "get_group",
    "get_ipset",
    "get_limits",
    "get_location_settings",
    "get_or_create",
    "group_rules",
    "init_for_new_vm",
    "ipset_entries",
    "list_groups",
    "list_ipsets",
    "record_readiness",
    "remove_foreign_references",
    "reorder_rules",
    "replace_rules",
    "service_rules",
    "set_admin_fields",
    "set_firewall",
    "update_baseline",
    "update_group",
    "update_ipset",
    "update_location_settings",
    "update_rule",
]
