"""Shared pieces of the VM firewall API surfaces (docs/vm-firewall.md section 10).

The client, admin, billing and reseller routes all answer with the views built here, so the
response shapes stay the same on every surface. Domain errors (``FirewallError``) are turned
into responses in one place, ``firewall_error_handler``, registered on the application.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.commerce_auth import _impersonated_by_from_token, _token_from_request
from app.models.location import Location
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import Service
from app.models.vm_firewall import (
    FirewallBaseline,
    FirewallGroup,
    FirewallIpset,
    FirewallLocationSettings,
    FirewallNicFlag,
    FirewallRule,
    FirewallRuleKind,
    ServiceFirewall,
)
from app.services.vm_firewall import presets as fw_presets
from app.services.vm_firewall import reconcile
from app.services.vm_firewall import service as svc
from app.services.vm_firewall.apply import Readiness, readiness
from app.services.vm_firewall.errors import (
    FirewallChanged,
    FirewallConflict,
    FirewallError,
    FirewallNotEligible,
)
from app.services.vm_firewall.proxmox_fw import (
    ClusterReader,
    PluginTransport,
    VmFirewallClient,
    net_firewall_flag,
)
from app.services.vm_firewall.render import (
    MARKER_PREFIX,
    load_context,
    render_context,
)

logger = logging.getLogger(__name__)

STAFF = "staff"
CUSTOMER = "customer"


# Errors


def firewall_error_body(exc: FirewallError) -> dict[str, Any]:
    """``{code, field, message, ...}`` plus ``detail`` (the message), which WHMCS reads."""
    body = exc.to_dict()
    if isinstance(exc, FirewallChanged):
        body["current_revision"] = exc.current_revision
    if isinstance(exc, FirewallNotEligible):
        body["reasons"] = list(exc.reasons)
    body["detail"] = exc.message
    return body


async def firewall_error_handler(request: Request, exc: FirewallError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=firewall_error_body(exc))


def interruption_not_confirmed() -> FirewallConflict:
    return FirewallConflict(
        "Turning the firewall on briefly interrupts this server's network (under half a second); "
        "send confirm_interruption=true to go ahead",
        code="interruption_confirmation_required",
        field="confirm_interruption",
    )


# Actors


def _request_meta(request: Optional[Request]) -> dict[str, Any]:
    if request is None:
        return {}
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def client_actor(request: Request, credentials, auth: dict) -> svc.Actor:
    """The client of the session; an impersonating admin is recorded as ``impersonated_by``."""
    impersonated_by = auth.get("impersonated_by") or _impersonated_by_from_token(
        _token_from_request(request, credentials)
    )
    return svc.Actor.client(int(auth["user_id"]), impersonated_by=impersonated_by, **_request_meta(request))


def staff_actor(request: Request, auth: dict) -> svc.Actor:
    user_id = auth.get("user_id")
    return svc.Actor.staff(int(user_id) if user_id else None, **_request_meta(request))


def billing_actor(request: Request, integration_id: int, service: Service) -> svc.Actor:
    return svc.Actor.billing(integration_id, service.owner_user_id, **_request_meta(request))


# Proxmox access (tests replace these two functions)


async def vm_firewall_client(db: Session, service: Service) -> VmFirewallClient:
    """The VM-only Proxmox firewall client of a VM service."""
    from app.services.proxmox_placement import resolve_proxmox_plugin_for_service

    plugin, _cluster_id, _node, vmid = await resolve_proxmox_plugin_for_service(db, service)
    return VmFirewallClient(PluginTransport(plugin), int(vmid))


def cluster_reader(db: Session, cluster: ProxmoxCluster) -> Optional[ClusterReader]:
    """A read-only cluster reader over the cluster's first enabled node; None without one."""
    from app.plugins.registry import get_registry
    from app.services.proxmox_placement import cluster_to_proxmox_plugin_config

    names = [str(n.node_name) for n in (cluster.nodes or []) if n.enabled and (n.node_name or "").strip()]
    if not names:
        return None
    config = cluster_to_proxmox_plugin_config(cluster, names[0], 0)
    return ClusterReader(PluginTransport(get_registry().get_plugin("proxmox", config)))


async def live_vm_config(db: Session, service: Service) -> Optional[dict[str, Any]]:
    """The VM's Proxmox config, or None when Proxmox cannot be read (the NIC checks are then
    skipped and the observed ``nic_flag`` is used)."""
    try:
        client = await vm_firewall_client(db, service)
        return await client.get_config()
    except Exception as exc:  # placement, transport and Proxmox errors
        logger.warning("[vm_firewall] could not read the Proxmox config of service %s: %s", service.id, exc)
        return None


async def check_cluster_readiness(db: Session, cluster: ProxmoxCluster) -> Readiness:
    try:
        reader = cluster_reader(db, cluster)
        if reader is None:
            return Readiness(ready=False, reasons=["cluster has no enabled node"])
        return await readiness(reader)
    except Exception as exc:  # transport failures and bad cluster records
        return Readiness(ready=False, reasons=[f"cluster unreadable: {exc}"])


# Views


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def rule_payload(rule: FirewallRule) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": rule.id,
        "position": rule.position,
        "kind": _value(rule.kind),
        "enabled": bool(rule.enabled),
        "comment": rule.comment,
    }
    if _value(rule.kind) == FirewallRuleKind.GROUP.value:
        out["group_id"] = rule.ref_group_id
        return out
    out.update(
        direction=_value(rule.direction),
        action=_value(rule.action),
        ip_version=_value(rule.ip_version),
        protocol=rule.protocol,
        ports=rule.ports,
        source_ports=rule.source_ports,
        icmp_type=rule.icmp_type,
        remote_cidrs=list(rule.remote_cidrs) if rule.remote_cidrs else None,
        remote_ipset_id=rule.remote_ipset_id,
        local_cidrs=list(rule.local_cidrs) if rule.local_cidrs else None,
        log=bool(rule.log),
    )
    return out


def _owned_rules(db: Session, column, owner_id: int) -> list[FirewallRule]:
    return list(
        db.execute(select(FirewallRule).where(column == owner_id).order_by(FirewallRule.position, FirewallRule.id))
        .scalars()
    )


def group_payload(db: Session, group: FirewallGroup) -> dict[str, Any]:
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "scope": _value(group.scope),
        "owner_user_id": group.owner_user_id,
        "client_selectable": bool(group.client_selectable),
        "revision": group.revision,
        "rules": [rule_payload(rule) for rule in _owned_rules(db, FirewallRule.group_id, group.id)],
    }


def ipset_payload(db: Session, ipset: FirewallIpset) -> dict[str, Any]:
    return {
        "id": ipset.id,
        "name": ipset.name,
        "description": ipset.description,
        "scope": _value(ipset.scope),
        "owner_user_id": ipset.owner_user_id,
        "client_selectable": bool(ipset.client_selectable),
        "purpose": _value(ipset.purpose),
        "revision": ipset.revision,
        "entries": [
            {"id": entry.id, "cidr": entry.cidr, "nomatch": bool(entry.nomatch), "comment": entry.comment}
            for entry in svc.ipset_entries(db, ipset.id)
        ],
    }


def baseline_payload(db: Session, baseline: FirewallBaseline) -> dict[str, Any]:
    return {
        "id": baseline.id,
        "name": baseline.name,
        "scope_type": _value(baseline.scope_type),
        "scope_id": baseline.scope_id,
        "enabled": bool(baseline.enabled),
        "settings": dict(baseline.settings or {}),
        "revision": baseline.revision,
        "rules": [rule_payload(rule) for rule in _owned_rules(db, FirewallRule.baseline_id, baseline.id)],
    }


def _location_row_values(row: Optional[FirewallLocationSettings]) -> dict[str, Any]:
    if row is None:
        return {
            "enabled": False,
            "nic_flag_on_provision": True,
            "enforce_baseline_on_all": False,
            "smtp_block_new_vms": True,
            "anti_spoof_new_vms": False,
            "ipv6_allowed_prefixes": [],
            "readiness": {},
            "readiness_checked_at": None,
            "rollout_paused_at": None,
            "rollout_pause_reason": None,
            "rollout_failures": 0,
        }
    return {
        "enabled": bool(row.enabled),
        "nic_flag_on_provision": bool(row.nic_flag_on_provision),
        "enforce_baseline_on_all": bool(row.enforce_baseline_on_all),
        "smtp_block_new_vms": bool(row.smtp_block_new_vms),
        "anti_spoof_new_vms": bool(row.anti_spoof_new_vms),
        "ipv6_allowed_prefixes": list(row.ipv6_allowed_prefixes or []),
        "readiness": dict(row.readiness or {}),
        "readiness_checked_at": _iso(row.readiness_checked_at),
        "rollout_paused_at": _iso(row.rollout_paused_at),
        "rollout_pause_reason": row.rollout_pause_reason,
        "rollout_failures": int(row.rollout_failures or 0),
    }


def location_payload(db: Session, location: Location) -> dict[str, Any]:
    row = db.get(FirewallLocationSettings, location.id)
    values = _location_row_values(row)
    checked = (values["readiness"].get("clusters") or {})
    clusters = db.execute(
        select(ProxmoxCluster).where(ProxmoxCluster.location_id == location.id).order_by(ProxmoxCluster.id)
    ).scalars()
    cluster_items = [
        {"id": cluster.id, "name": cluster.name, "readiness": checked.get(str(cluster.id))} for cluster in clusters
    ]
    names = {str(item["id"]): item["name"] for item in cluster_items}
    values["readiness"] = {
        **values["readiness"],
        "clusters": {key: {**entry, "name": names.get(key)} for key, entry in checked.items()},
    }
    ready = bool(cluster_items) and all((item["readiness"] or {}).get("ready") for item in cluster_items)
    return {
        "location_id": location.id,
        "name": location.name,
        **values,
        "clusters": cluster_items,
        "ready": ready,
        "ipv6_allowed_prefixes_help": svc.IPV6_ALLOWED_PREFIXES_HELP,
    }


def _cluster_ready(location_settings: Optional[FirewallLocationSettings], cluster_id: Optional[int]) -> bool:
    if location_settings is None or cluster_id is None:
        return False
    clusters = (location_settings.readiness or {}).get("clusters") or {}
    return bool((clusters.get(str(cluster_id)) or {}).get("ready"))


_STATE_MESSAGES = {
    "pending": "Saving the firewall to the server.",
    "pending_reboot": "Some firewall changes take effect after the server is next restarted.",
    "error": "The last change could not be applied yet; it is retried automatically.",
    "drift": "The firewall on the server differed from these settings and is being repaired.",
    "not_enforced": "The firewall is not enforced at the moment.",
    "unmanaged": "The firewall of this server is managed by staff.",
    "vm_missing": "The server could not be found on its host.",
}
_REASON_MESSAGES = {
    "ipv6_ra_bridge_needs_prefix": "Address anti-spoofing is off until IPv6 prefixes are set for this location.",
    "net0_bridge_unknown": "Address anti-spoofing starts once the server's network has been checked.",
}

_SMTP_MARKER = MARKER_PREFIX + "smtp"
_MGMT_MARKER = MARKER_PREFIX + "mgmt"


def _baseline_view(db: Session, ctx, rendered) -> dict[str, Any]:
    """The baseline as the client sees it, read only: the admin baseline rules in merge order
    (``baseline_id`` says which), then the generated SMTP and management blocks."""
    rules: list[dict[str, Any]] = []
    for baseline in ctx.baselines:
        for rule in _owned_rules(db, FirewallRule.baseline_id, baseline.id):
            item = {**rule_payload(rule), "baseline_id": baseline.id, "generated": False}
            if item.get("group_id") is not None:
                group = db.get(FirewallGroup, item["group_id"])
                item["group_name"] = group.name if group is not None else None
            if item.get("remote_ipset_id") is not None:
                ipset = db.get(FirewallIpset, item["remote_ipset_id"])
                item["remote_ipset_name"] = ipset.name if ipset is not None else None
            rules.append(item)
    generated = [rule for rule in rendered.rules[: rendered.baseline_rule_count]
                 if rule.comment.startswith((_SMTP_MARKER, _MGMT_MARKER))]
    for rule in generated:
        smtp = rule.comment.startswith(_SMTP_MARKER)
        rules.append({
            "kind": "rule",
            "direction": rule.type,
            "action": rule.action,
            "protocol": rule.proto,
            "ports": rule.dport,
            "enabled": True,
            "generated": True,
            "comment": "Outgoing mail on port 25 blocked" if smtp else "Management networks blocked",
        })
    return {
        "anti_spoof": int(rendered.options.get("ipfilter", 0)) == 1,
        "mac_filter": int(rendered.options.get("macfilter", 0)) == 1,
        "smtp_out": "block" if any(rule.comment.startswith(_SMTP_MARKER) for rule in generated) else "allow",
        "management_blocked": any(rule.comment.startswith(_MGMT_MARKER) for rule in generated),
        "client_may_disable": bool(rendered.settings.get("client_may_disable", True)),
        "rules": rules,
    }


def _needs_interruption(row, config: Optional[dict[str, Any]]) -> bool:
    """Whether turning the client firewall on now would write net0's ``firewall`` flag (the short
    blip of section 5.2). Without the live config the flag is unknown, which counts as yes even
    when the stored flag says it is set."""
    if row.client_enabled:
        return False
    if config is None:
        return True
    return not net_firewall_flag(config.get("net0"))


def _view_needs_interruption(row) -> bool:
    """The view's hint, from the stored flag (reads do not call Proxmox); the write checks live."""
    if row.client_enabled:
        return False
    return _value(row.nic_flag) != FirewallNicFlag.ON.value


def firewall_available(db: Session, service: Service) -> bool:
    """Whether the client surfaces show the firewall of this service (the view's ``available``
    for a client); reads only, creates no firewall row."""
    if _value(service.service_type) != "vm" or service.vm is None:
        return False
    if not svc.eligibility(db, service.id, observe=False).eligible:
        return False
    cluster = db.get(ProxmoxCluster, service.vm.proxmox_cluster_id)
    location = db.get(FirewallLocationSettings, cluster.location_id) if cluster and cluster.location_id else None
    return _cluster_ready(location, service.vm.proxmox_cluster_id)


def firewall_view(db: Session, service_id: int, *, audience: str = CUSTOMER) -> dict[str, Any]:
    """The firewall of a VM as section 10.1 describes it; ``audience`` ``staff`` adds the admin
    fields. Writes nothing: a VM with no firewall row yet is shown with the defaults."""
    service = db.get(Service, service_id)
    if service is None or _value(service.service_type) != "vm":
        raise FirewallNotEligible(["not_vm"])
    row = db.get(ServiceFirewall, service_id) or svc.default_firewall(service_id)
    ctx = load_context(db, service_id)
    rendered = render_context(db, ctx)
    result = svc.eligibility(db, service_id, observe=False)
    cluster_id = ctx.vm.proxmox_cluster_id if ctx.vm is not None else None
    cluster_ready = _cluster_ready(ctx.location_settings, cluster_id)
    reasons = list(result.reasons)
    held = [reason for reason in reconcile.held_reasons(row) if reason not in reasons]
    reasons.extend(held)
    if ctx.location_settings is not None and ctx.location_settings.enabled and not cluster_ready:
        reasons.append("cluster_not_ready")
    # A location switched on in staging (readiness not passed) stays hidden from clients.
    available = result.eligible and not held and (cluster_ready or audience == STAFF)
    reasons.extend(rendered.reasons)
    state = _value(row.state)
    if not available:
        message = "The firewall is not available for this server."
    else:
        message = _STATE_MESSAGES.get(state)
        if message is None and rendered.reasons:
            message = _REASON_MESSAGES.get(rendered.reasons[0])
    rules = svc.service_rules(db, service_id)
    group_ids = [rule.ref_group_id for rule in rules if _value(rule.kind) == FirewallRuleKind.GROUP.value]
    groups = [db.get(FirewallGroup, gid) for gid in group_ids]
    baseline = _baseline_view(db, ctx, rendered)
    view: dict[str, Any] = {
        "service_id": service_id,
        "available": available,
        "reasons": reasons,
        "location_enabled": bool(ctx.location_settings and ctx.location_settings.enabled),
        "client_enabled": bool(row.client_enabled),
        "state": state,
        "enforced": bool(rendered.enforced and int(rendered.options["enable"]) == 1),
        "nic_flag": _value(row.nic_flag),
        "needs_interruption": _view_needs_interruption(row),
        "policy_in": _value(row.policy_in),
        "policy_out": _value(row.policy_out),
        "log_level_in": _value(row.log_level_in),
        "log_level_out": _value(row.log_level_out),
        "rules": [rule_payload(rule) for rule in rules],
        "groups": [group_payload(db, group) for group in groups if group is not None],
        "ipsets": ipset_choices(db, ctx.service, audience=audience),
        "baseline": baseline,
        "smtp_block_help": svc.SMTP_BLOCK_HELP if baseline["smtp_out"] == "block" else None,
        "limits": dataclasses.asdict(svc.get_limits(db)),
        "suggested_presets": list(fw_presets.suggested_presets(db, service_id)),
        "revision": row.revision,
        "last_applied_at": _iso(row.last_applied_at),
        "message": message,
    }
    if audience == STAFF:
        view.update(
            managed=bool(row.managed),
            excluded=bool(row.excluded),
            enforce_baseline=bool(row.enforce_baseline),
            admin_overrides=dict(row.admin_overrides or {}),
            location_id=ctx.location_id,
            cluster_id=cluster_id,
            cluster_ready=cluster_ready,
            nics_checked=result.nics_checked,
            net0_bridge=row.net0_bridge,
            rollout_after=_iso(row.rollout_after),
            applied_revision=row.applied_revision,
            desired_hash=rendered.desired_hash,
            applied_hash=row.applied_hash,
            in_sync=row.applied_hash == rendered.desired_hash,
            last_error=row.last_error,
            failing_since=_iso(row.failing_since),
            last_checked_at=_iso(row.last_checked_at),
            next_attempt_at=_iso(row.next_attempt_at),
            drift_repairs_24h=row.drift_repairs_24h,
            rendered={"options": dict(rendered.options), "rules": [rule.params() for rule in rendered.rules]},
        )
    return view


def ipset_choices(db: Session, service: Service, *, audience: str = CUSTOMER) -> list[dict[str, Any]]:
    """IP sets a rule of this VM may reference: the owner's own plus client-selectable admin sets,
    and for staff every admin set."""
    items: list[FirewallIpset] = []
    if service.owner_user_id is not None:
        items = svc.list_ipsets(db, svc.Actor.client(service.owner_user_id))
    if audience == STAFF:
        seen = {ipset.id for ipset in items}
        admin_sets = db.execute(
            select(FirewallIpset).where(FirewallIpset.owner_user_id.is_(None)).order_by(FirewallIpset.name)
        ).scalars()
        items += [ipset for ipset in admin_sets if ipset.id not in seen]
    return [
        {"id": ipset.id, "name": ipset.name, "scope": _value(ipset.scope), "purpose": _value(ipset.purpose),
         "description": ipset.description}
        for ipset in items
    ]


def presets_payload() -> list[dict[str, Any]]:
    return [
        {
            "key": preset.key,
            "label": preset.label,
            "description": preset.description,
            "rules": [dict(rule) for rule in preset.rules],
            "policy_in": preset.policy_in,
        }
        for preset in fw_presets.PRESETS.values()
    ]


# Request models shared by the client and billing routes. Unknown fields (among them the
# admin-only ones) are refused with 422.


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Revision = Field(..., description="The `revision` of the firewall as last read")


class FirewallSettingsUpdate(RequestModel):
    revision: StrictInt = Revision
    client_enabled: Optional[StrictBool] = None
    policy_in: Optional[str] = None
    policy_out: Optional[str] = None
    log_level_in: Optional[str] = None
    log_level_out: Optional[str] = None
    confirm_interruption: StrictBool = False


class RulesReplace(RequestModel):
    revision: StrictInt = Revision
    rules: list[dict[str, Any]]


class RuleCreate(RequestModel):
    revision: StrictInt = Revision
    rule: dict[str, Any]
    position: Optional[StrictInt] = None


class RuleUpdate(RequestModel):
    revision: StrictInt = Revision
    rule: dict[str, Any]


class RulesOrder(RequestModel):
    revision: StrictInt = Revision
    ids: list[StrictInt]


class RevisionOnly(RequestModel):
    revision: StrictInt = Revision


class GroupAttach(RequestModel):
    revision: StrictInt = Revision
    position: Optional[StrictInt] = None


class GroupCreate(RequestModel):
    name: str
    description: Optional[str] = None
    rules: list[dict[str, Any]] = Field(default_factory=list)


class GroupUpdate(RequestModel):
    revision: Optional[StrictInt] = None
    name: Optional[str] = None
    description: Optional[str] = None
    rules: Optional[list[dict[str, Any]]] = None


class IpsetCreate(RequestModel):
    name: str
    description: Optional[str] = None
    entries: list[Any] = Field(default_factory=list)


class IpsetUpdate(RequestModel):
    revision: Optional[StrictInt] = None
    name: Optional[str] = None
    description: Optional[str] = None
    entries: Optional[list[Any]] = None


def given(body: BaseModel, *keys: str) -> dict[str, Any]:
    """The fields the caller sent (``UNSET`` for the rest), for the service's partial updates."""
    return {key: getattr(body, key) for key in keys if key in body.model_fields_set}


# Write handlers shared by the client, billing and admin routes. They leave committing to the
# route.


async def update_settings(db: Session, service: Service, actor: svc.Actor, body: FirewallSettingsUpdate) -> None:
    # The checks below read without creating the row, so a refusal writes nothing.
    row = db.get(ServiceFirewall, service.id) or svc.default_firewall(service.id)
    if body.revision != row.revision:
        raise FirewallChanged(row.revision)
    config = None
    if body.client_enabled is True and not row.client_enabled:
        config = await live_vm_config(db, service)
        # Eligibility first, so a VM that cannot have a firewall (its location switched off, for
        # one) is told so rather than asked to confirm an interruption that would not happen.
        result = svc.eligibility(db, service.id, vm_config=config, observe=False)
        if not result.eligible:
            raise FirewallNotEligible(result.reasons)
        if _needs_interruption(row, config) and not body.confirm_interruption:
            raise interruption_not_confirmed()
    svc.set_firewall(
        db,
        service.id,
        actor,
        revision=body.revision,
        client_enabled=body.client_enabled,
        policy_in=body.policy_in,
        policy_out=body.policy_out,
        log_level_in=body.log_level_in,
        log_level_out=body.log_level_out,
        vm_config=config,
    )


def replace_rules(db: Session, service: Service, actor: svc.Actor, body: RulesReplace) -> None:
    svc.replace_rules(db, service.id, actor, body.rules, revision=body.revision)


def add_rule(db: Session, service: Service, actor: svc.Actor, body: RuleCreate) -> None:
    svc.add_rule(db, service.id, actor, body.rule, revision=body.revision, position=body.position)


def update_rule(db: Session, service: Service, actor: svc.Actor, rule_id: int, body: RuleUpdate) -> None:
    svc.update_rule(db, service.id, actor, rule_id, body.rule, revision=body.revision)


def delete_rule(db: Session, service: Service, actor: svc.Actor, rule_id: int, revision: int) -> None:
    svc.delete_rule(db, service.id, actor, rule_id, revision=revision)


def reorder_rules(db: Session, service: Service, actor: svc.Actor, body: RulesOrder) -> None:
    svc.reorder_rules(db, service.id, actor, body.ids, revision=body.revision)


def apply_preset(db: Session, service: Service, actor: svc.Actor, preset: str, body: RevisionOnly) -> None:
    svc.apply_preset(db, service.id, actor, preset, revision=body.revision)


def attach_group(db: Session, service: Service, actor: svc.Actor, group_id: int, body: GroupAttach) -> None:
    svc.attach_group(db, service.id, actor, group_id, revision=body.revision, position=body.position)


def detach_group(db: Session, service: Service, actor: svc.Actor, group_id: int, revision: int) -> None:
    svc.detach_group(db, service.id, actor, group_id, revision=revision)


def owner_groups(db: Session, service: Service) -> list[dict[str, Any]]:
    """Groups the service's owner may attach: their own and admin groups marked client selectable."""
    if service.owner_user_id is None:
        return []
    actor = svc.Actor.client(service.owner_user_id)
    return [group_payload(db, group) for group in svc.list_groups(db, actor)]
