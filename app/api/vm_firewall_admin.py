"""Admin VM firewall API under ``/api/admin/vm-firewall`` (docs/vm-firewall.md section 10.2).

Locations (settings, readiness, staged rollout), baselines, admin groups and IP sets, the status
list, and every VM's firewall with the admin-only fields. Lists are bare JSON arrays.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import Field, StrictBool, StrictInt
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api import vm_firewall_common as fw
from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import Service, ServiceType
from app.models.service_vm import ServiceVm
from app.models.user import User
from app.models.vm_firewall import (
    FirewallBaseline,
    FirewallGroup,
    FirewallIpset,
    FirewallObjectScope,
    FirewallState,
    ServiceFirewall,
)
from app.services.vm_firewall import apply as fw_apply
from app.services.vm_firewall import reconcile
from app.services.vm_firewall import service as svc
from app.services.vm_firewall.errors import FirewallChanged, FirewallNotEligible, FirewallNotFound
from app.services.vm_firewall.render import load_context

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]
RevisionQuery = Annotated[int, Query(description="The `revision` as last read")]

router = APIRouter(prefix="/admin/vm-firewall", tags=["admin-vm-firewall"])


def _actor(request: Request, auth: dict) -> svc.Actor:
    return fw.staff_actor(request, auth)


# Request models (admin only: never accepted by the client or billing routes)


class AdminFirewallUpdate(fw.FirewallSettingsUpdate):
    managed: Optional[StrictBool] = None
    excluded: Optional[StrictBool] = None
    enforce_baseline: Optional[StrictBool] = None
    admin_overrides: Optional[dict[str, Any]] = None


_SETTINGS_FIELDS = ("client_enabled", "policy_in", "policy_out", "log_level_in", "log_level_out")
_ADMIN_FIELDS = ("managed", "excluded", "enforce_baseline", "admin_overrides")


class LocationUpdate(fw.RequestModel):
    enabled: Optional[StrictBool] = None
    nic_flag_on_provision: Optional[StrictBool] = None
    enforce_baseline_on_all: Optional[StrictBool] = None
    smtp_block_new_vms: Optional[StrictBool] = None
    anti_spoof_new_vms: Optional[StrictBool] = None
    ipv6_allowed_prefixes: Optional[list[str]] = None
    staging: StrictBool = False
    confirm_existing_vms: Optional[StrictInt] = None


class RolloutRequest(fw.RequestModel):
    dry_run: StrictBool = True
    confirm_existing_vms: Optional[StrictInt] = None


class BaselineCreate(fw.RequestModel):
    name: str
    scope_type: str
    scope_id: Optional[StrictInt] = None
    settings: Optional[dict[str, Any]] = None
    rules: list[dict[str, Any]] = Field(default_factory=list)
    enabled: StrictBool = True


class BaselineUpdate(fw.RequestModel):
    revision: Optional[StrictInt] = None
    name: Optional[str] = None
    settings: Optional[dict[str, Any]] = None
    rules: Optional[list[dict[str, Any]]] = None
    enabled: Optional[StrictBool] = None


class AdminGroupCreate(fw.GroupCreate):
    owner_user_id: Optional[StrictInt] = None
    client_selectable: StrictBool = False


class AdminGroupUpdate(fw.GroupUpdate):
    client_selectable: Optional[StrictBool] = None


class AdminIpsetCreate(fw.IpsetCreate):
    owner_user_id: Optional[StrictInt] = None
    client_selectable: StrictBool = False
    purpose: str = "general"


class AdminIpsetUpdate(fw.IpsetUpdate):
    client_selectable: Optional[StrictBool] = None
    purpose: Optional[str] = None


# Locations


def _location(db: Session, location_id: int) -> Location:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    return location


def _preview(db: Session, location_id: int) -> dict[str, Any]:
    preview = svc.preview_enforce_baseline_on_all(db, location_id)
    ids = preview["service_ids"]
    names = dict(db.execute(select(Service.id, Service.name).where(Service.id.in_(ids))).all()) if ids else {}
    preview["services"] = [{"id": sid, "name": names.get(sid)} for sid in ids]
    return preview


@router.get("/locations", responses=COMMON_ERROR_RESPONSES)
def list_locations(auth: AdminDep, db: DbDep):
    locations = db.execute(select(Location).order_by(Location.name, Location.id)).scalars()
    return [fw.location_payload(db, location) for location in locations]


@router.get("/locations/{location_id}", responses=COMMON_ERROR_RESPONSES)
def get_location(location_id: int, auth: AdminDep, db: DbDep):
    return fw.location_payload(db, _location(db, location_id))


@router.put("/locations/{location_id}", responses=COMMON_ERROR_RESPONSES)
def update_location(location_id: int, body: LocationUpdate, request: Request, auth: AdminDep, db: DbDep):
    """Answers 409 ``confirmation_required`` with a fresh ``preview`` when the change reaches
    existing VMs and ``confirm_existing_vms`` is not the previewed count."""
    location = _location(db, location_id)
    svc.update_location_settings(
        db,
        _actor(request, auth),
        location.id,
        enabled=body.enabled,
        nic_flag_on_provision=body.nic_flag_on_provision,
        enforce_baseline_on_all=body.enforce_baseline_on_all,
        smtp_block_new_vms=body.smtp_block_new_vms,
        anti_spoof_new_vms=body.anti_spoof_new_vms,
        staging=body.staging,
        confirm_existing_vms=body.confirm_existing_vms,
        **fw.given(body, "ipv6_allowed_prefixes"),
    )
    db.commit()
    return fw.location_payload(db, location)


@router.post("/locations/{location_id}/readiness", responses=COMMON_ERROR_RESPONSES)
async def check_location_readiness(location_id: int, auth: AdminDep, db: DbDep):
    """Re-run the read-only readiness check (section 9.2) for every cluster of the location."""
    location = _location(db, location_id)
    clusters = db.execute(select(ProxmoxCluster).where(ProxmoxCluster.location_id == location.id)).scalars().all()
    for cluster in clusters:
        result = await fw.check_cluster_readiness(db, cluster)
        svc.record_readiness(db, location.id, cluster.id, result.to_dict())
    db.commit()
    return fw.location_payload(db, location)


@router.post("/locations/{location_id}/rollout", responses=COMMON_ERROR_RESPONSES)
def rollout(location_id: int, body: RolloutRequest, request: Request, auth: AdminDep, db: DbDep):
    """``dry_run`` (the default) returns the preview of switching ``enforce_baseline_on_all`` on;
    otherwise it switches it on, which needs ``confirm_existing_vms`` equal to the preview's
    ``changed``."""
    location = _location(db, location_id)
    if body.dry_run:
        return {"dry_run": True, "preview": _preview(db, location.id), "location": fw.location_payload(db, location)}
    preview = _preview(db, location.id)
    svc.update_location_settings(
        db, _actor(request, auth), location.id, enforce_baseline_on_all=True,
        confirm_existing_vms=body.confirm_existing_vms,
    )
    db.commit()
    return {"dry_run": False, "preview": preview, "location": fw.location_payload(db, location)}


@router.post("/locations/{location_id}/rollout/resume", responses=COMMON_ERROR_RESPONSES)
def resume_rollout(location_id: int, request: Request, auth: AdminDep, db: DbDep):
    """Resume a rollout the worker paused after repeated failures; the VMs still waiting are
    staggered again from now. Returns the location."""
    location = _location(db, location_id)
    reconcile.resume_rollout(db, _actor(request, auth), location.id)
    db.commit()
    return fw.location_payload(db, location)


@router.get("/worker", responses=COMMON_ERROR_RESPONSES)
def worker_status(auth: AdminDep, db: DbDep):
    """The firewall worker's last reported status (``last_tick_at``, ``owner`` and the counters it
    writes); an empty object before its first tick."""
    return reconcile.read_status(db)


# Baselines


@router.get("/baselines", responses=COMMON_ERROR_RESPONSES)
def list_baselines(auth: AdminDep, db: DbDep):
    rows = db.execute(select(FirewallBaseline).order_by(FirewallBaseline.id)).scalars()
    return [fw.baseline_payload(db, row) for row in rows]


@router.post("/baselines", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_baseline(body: BaselineCreate, request: Request, auth: AdminDep, db: DbDep):
    row = svc.create_baseline(
        db, _actor(request, auth), name=body.name, scope_type=body.scope_type, scope_id=body.scope_id,
        settings=body.settings, rules=body.rules, enabled=body.enabled,
    )
    db.commit()
    return fw.baseline_payload(db, row)


def _baseline(db: Session, baseline_id: int) -> FirewallBaseline:
    row = db.get(FirewallBaseline, baseline_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Baseline not found")
    return row


@router.get("/baselines/{baseline_id}", responses=COMMON_ERROR_RESPONSES)
def get_baseline(baseline_id: int, auth: AdminDep, db: DbDep):
    return fw.baseline_payload(db, _baseline(db, baseline_id))


@router.put("/baselines/{baseline_id}", responses=COMMON_ERROR_RESPONSES)
def update_baseline(baseline_id: int, body: BaselineUpdate, request: Request, auth: AdminDep, db: DbDep):
    _baseline(db, baseline_id)
    row = svc.update_baseline(
        db, _actor(request, auth), baseline_id, revision=body.revision,
        **fw.given(body, "name", "settings", "rules", "enabled"),
    )
    db.commit()
    return fw.baseline_payload(db, row)


@router.delete("/baselines/{baseline_id}", status_code=status.HTTP_204_NO_CONTENT,
               responses=COMMON_ERROR_RESPONSES)
def delete_baseline(baseline_id: int, request: Request, auth: AdminDep, db: DbDep, revision: Optional[int] = None):
    _baseline(db, baseline_id)
    svc.delete_baseline(db, _actor(request, auth), baseline_id, revision=revision)
    db.commit()


# Groups and IP sets (admin ones, and any client's)


def _scoped(query, model, scope: Optional[str], owner_user_id: Optional[int]):
    if scope is not None:
        query = query.where(model.scope == FirewallObjectScope(scope))
    if owner_user_id is not None:
        query = query.where(model.owner_user_id == owner_user_id)
    return query


ScopeQuery = Annotated[Optional[str], Query(pattern="^(admin|client)$")]


@router.get("/groups", responses=COMMON_ERROR_RESPONSES)
def list_groups(auth: AdminDep, db: DbDep, scope: ScopeQuery = None, owner_user_id: Optional[int] = None):
    query = _scoped(select(FirewallGroup).order_by(FirewallGroup.name, FirewallGroup.id), FirewallGroup, scope,
                    owner_user_id)
    return [fw.group_payload(db, group) for group in db.execute(query).scalars()]


@router.post("/groups", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_group(body: AdminGroupCreate, request: Request, auth: AdminDep, db: DbDep):
    group = svc.create_group(
        db, _actor(request, auth), name=body.name, description=body.description, rules=body.rules,
        owner_user_id=body.owner_user_id, client_selectable=body.client_selectable,
    )
    db.commit()
    return fw.group_payload(db, group)


@router.get("/groups/{group_id}", responses=COMMON_ERROR_RESPONSES)
def get_group(group_id: int, request: Request, auth: AdminDep, db: DbDep):
    return fw.group_payload(db, svc.get_group(db, _actor(request, auth), group_id))


@router.put("/groups/{group_id}", responses=COMMON_ERROR_RESPONSES)
def update_group(group_id: int, body: AdminGroupUpdate, request: Request, auth: AdminDep, db: DbDep):
    group = svc.update_group(
        db, _actor(request, auth), group_id, revision=body.revision,
        **fw.given(body, "name", "description", "rules", "client_selectable"),
    )
    db.commit()
    return fw.group_payload(db, group)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
def delete_group(
    group_id: int, request: Request, auth: AdminDep, db: DbDep, revision: Optional[int] = None, detach: bool = False
):
    svc.delete_group(db, _actor(request, auth), group_id, revision=revision, detach=detach)
    db.commit()


@router.get("/ipsets", responses=COMMON_ERROR_RESPONSES)
def list_ipsets(
    auth: AdminDep,
    db: DbDep,
    scope: ScopeQuery = None,
    owner_user_id: Optional[int] = None,
    purpose: Annotated[Optional[str], Query(pattern="^(general|management)$")] = None,
):
    query = _scoped(select(FirewallIpset).order_by(FirewallIpset.name, FirewallIpset.id), FirewallIpset, scope,
                    owner_user_id)
    rows = [row for row in db.execute(query).scalars() if purpose is None or fw._value(row.purpose) == purpose]
    return [fw.ipset_payload(db, row) for row in rows]


@router.post("/ipsets", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_ipset(body: AdminIpsetCreate, request: Request, auth: AdminDep, db: DbDep):
    ipset = svc.create_ipset(
        db, _actor(request, auth), name=body.name, description=body.description, entries=body.entries,
        owner_user_id=body.owner_user_id, client_selectable=body.client_selectable, purpose=body.purpose,
    )
    db.commit()
    return fw.ipset_payload(db, ipset)


@router.get("/ipsets/{ipset_id}", responses=COMMON_ERROR_RESPONSES)
def get_ipset(ipset_id: int, request: Request, auth: AdminDep, db: DbDep):
    return fw.ipset_payload(db, svc.get_ipset(db, _actor(request, auth), ipset_id))


@router.put("/ipsets/{ipset_id}", responses=COMMON_ERROR_RESPONSES)
def update_ipset(ipset_id: int, body: AdminIpsetUpdate, request: Request, auth: AdminDep, db: DbDep):
    ipset = svc.update_ipset(
        db, _actor(request, auth), ipset_id, revision=body.revision,
        **fw.given(body, "name", "description", "entries", "client_selectable", "purpose"),
    )
    db.commit()
    return fw.ipset_payload(db, ipset)


@router.delete("/ipsets/{ipset_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
def delete_ipset(ipset_id: int, request: Request, auth: AdminDep, db: DbDep, revision: Optional[int] = None):
    svc.delete_ipset(db, _actor(request, auth), ipset_id, revision=revision)
    db.commit()


# VM firewalls


@router.get("/services", responses=COMMON_ERROR_RESPONSES)
def list_services(
    auth: AdminDep,
    db: DbDep,
    state: Optional[FirewallState] = None,
    location_id: Optional[int] = None,
    q: Annotated[Optional[str], Query(max_length=100)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """VM services with their firewall status; a VM never opened has no row and shows ``off``."""
    query = (
        select(Service, ServiceFirewall, ServiceVm, ProxmoxCluster, Location, User)
        .join(ServiceVm, ServiceVm.service_id == Service.id)
        .outerjoin(ServiceFirewall, ServiceFirewall.service_id == Service.id)
        .outerjoin(ProxmoxCluster, ProxmoxCluster.id == ServiceVm.proxmox_cluster_id)
        .outerjoin(Location, Location.id == ProxmoxCluster.location_id)
        .outerjoin(User, User.id == Service.owner_user_id)
        .where(Service.service_type == ServiceType.VM)
    )
    if state is not None:
        condition = ServiceFirewall.state == state
        if state == FirewallState.OFF:
            condition = or_(condition, ServiceFirewall.service_id.is_(None))
        query = query.where(condition)
    if location_id is not None:
        query = query.where(ProxmoxCluster.location_id == location_id)
    if q:
        like = f"%{q.strip()}%"
        terms = [Service.name.ilike(like), User.username.ilike(like), User.email.ilike(like)]
        if q.strip().isdigit():
            terms.append(Service.id == int(q.strip()))
        query = query.where(or_(*terms))
    rows = db.execute(query.order_by(Service.id.desc()).offset(offset).limit(limit)).all()
    return [
        {
            "service_id": service.id,
            "service_name": service.name,
            "owner_user_id": service.owner_user_id,
            "owner_username": user.username if user is not None else None,
            "location_id": location.id if location is not None else None,
            "location_name": location.name if location is not None else None,
            "cluster_id": vm.proxmox_cluster_id,
            "node": vm.proxmox_node_name,
            "vmid": vm.proxmox_vmid,
            "state": fw._value(row.state) if row is not None else FirewallState.OFF.value,
            "client_enabled": bool(row.client_enabled) if row is not None else False,
            "managed": bool(row.managed) if row is not None else True,
            "excluded": bool(row.excluded) if row is not None else False,
            "nic_flag": fw._value(row.nic_flag) if row is not None else "unknown",
            "revision": row.revision if row is not None else None,
            "applied_revision": row.applied_revision if row is not None else None,
            "last_applied_at": fw._iso(row.last_applied_at) if row is not None else None,
            "last_error": row.last_error if row is not None else None,
        }
        for service, row, vm, _cluster, location, user in rows
    ]


def _service(db: Session, service_id: int) -> Service:
    service = ServiceDAO.get_by_id(db, service_id)
    if service is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return service


def _done(db: Session, service: Service) -> dict[str, Any]:
    db.commit()
    return fw.firewall_view(db, service.id, audience=fw.STAFF)


_SERVICE = "/services/{service_id}"


@router.get(_SERVICE, responses=COMMON_ERROR_RESPONSES)
def get_firewall(service_id: int, auth: AdminDep, db: DbDep):
    return fw.firewall_view(db, _service(db, service_id).id, audience=fw.STAFF)


@router.put(_SERVICE, responses=COMMON_ERROR_RESPONSES)
async def update_firewall(service_id: int, body: AdminFirewallUpdate, request: Request, auth: AdminDep, db: DbDep):
    """The client fields and the admin-only ``managed``, ``excluded``, ``enforce_baseline`` and
    ``admin_overrides``, under one ``revision``."""
    service = _service(db, service_id)
    actor = _actor(request, auth)
    row = db.get(ServiceFirewall, service.id) or svc.default_firewall(service.id)
    if body.revision != row.revision:
        raise FirewallChanged(row.revision)
    changed_settings = any(key in body.model_fields_set for key in _SETTINGS_FIELDS)
    if changed_settings:
        await fw.update_settings(db, service, actor, body)
    admin = fw.given(body, *_ADMIN_FIELDS)
    if admin:
        # After a settings change the row is locked by this transaction, so its new revision is
        # the one to compare against; otherwise the caller's revision is checked under the lock.
        current = svc.get_or_create(db, service.id).revision if changed_settings else body.revision
        svc.set_admin_fields(db, service.id, actor, revision=current, **admin)
    return _done(db, service)


@router.put(_SERVICE + "/rules", responses=COMMON_ERROR_RESPONSES)
def replace_rules(service_id: int, body: fw.RulesReplace, request: Request, auth: AdminDep, db: DbDep):
    service = _service(db, service_id)
    fw.replace_rules(db, service, _actor(request, auth), body)
    return _done(db, service)


@router.post(_SERVICE + "/rules", responses=COMMON_ERROR_RESPONSES)
def add_rule(service_id: int, body: fw.RuleCreate, request: Request, auth: AdminDep, db: DbDep):
    service = _service(db, service_id)
    fw.add_rule(db, service, _actor(request, auth), body)
    return _done(db, service)


@router.post(_SERVICE + "/rules/order", responses=COMMON_ERROR_RESPONSES)
def reorder_rules(service_id: int, body: fw.RulesOrder, request: Request, auth: AdminDep, db: DbDep):
    service = _service(db, service_id)
    fw.reorder_rules(db, service, _actor(request, auth), body)
    return _done(db, service)


@router.put(_SERVICE + "/rules/{rule_id}", responses=COMMON_ERROR_RESPONSES)
def update_rule(service_id: int, rule_id: int, body: fw.RuleUpdate, request: Request, auth: AdminDep, db: DbDep):
    service = _service(db, service_id)
    fw.update_rule(db, service, _actor(request, auth), rule_id, body)
    return _done(db, service)


@router.delete(_SERVICE + "/rules/{rule_id}", responses=COMMON_ERROR_RESPONSES)
def delete_rule(service_id: int, rule_id: int, revision: RevisionQuery, request: Request, auth: AdminDep, db: DbDep):
    service = _service(db, service_id)
    fw.delete_rule(db, service, _actor(request, auth), rule_id, revision)
    return _done(db, service)


@router.post(_SERVICE + "/presets/{preset}", responses=COMMON_ERROR_RESPONSES)
def apply_preset(service_id: int, preset: str, body: fw.RevisionOnly, request: Request, auth: AdminDep, db: DbDep):
    service = _service(db, service_id)
    fw.apply_preset(db, service, _actor(request, auth), preset, body)
    return _done(db, service)


@router.post(_SERVICE + "/groups/{group_id}", responses=COMMON_ERROR_RESPONSES)
def attach_group(service_id: int, group_id: int, body: fw.GroupAttach, request: Request, auth: AdminDep, db: DbDep):
    service = _service(db, service_id)
    fw.attach_group(db, service, _actor(request, auth), group_id, body)
    return _done(db, service)


@router.delete(_SERVICE + "/groups/{group_id}", responses=COMMON_ERROR_RESPONSES)
def detach_group(
    service_id: int, group_id: int, revision: RevisionQuery, request: Request, auth: AdminDep, db: DbDep
):
    service = _service(db, service_id)
    fw.detach_group(db, service, _actor(request, auth), group_id, revision)
    return _done(db, service)


async def _proxmox(db: Session, service: Service):
    try:
        return await fw.vm_firewall_client(db, service)
    except Exception as exc:  # placement errors and unreachable clusters
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Proxmox is not reachable: {exc}")


@router.post(_SERVICE + "/apply", responses=COMMON_ERROR_RESPONSES)
async def apply_now(service_id: int, request: Request, auth: AdminDep, db: DbDep):
    """Apply the rendered firewall to Proxmox now; failures are recorded on the row like the worker's.
    In a location whose firewall is off it is refused (409 ``not_eligible``, ``location_disabled``)
    unless the VM was applied before, when it can only switch the firewall off, as the worker does."""
    service = _service(db, service_id)
    row = svc.get_or_create(db, service.id)
    location = load_context(db, service.id).location_settings
    if not (location is not None and location.enabled) and row.applied_revision is None:
        raise FirewallNotEligible(["location_disabled"])
    client = await _proxmox(db, service)
    svc._audit(db, _actor(request, auth), "firewall.apply_requested", resource_type="service",
               resource_id=service.id, subject_user_id=service.owner_user_id, metadata={"revision": row.revision})
    result = await fw_apply.apply(db, service.id, client)
    db.commit()
    return {"result": dataclasses.asdict(result), "firewall": fw.firewall_view(db, service.id, audience=fw.STAFF)}


@router.post(_SERVICE + "/check", responses=COMMON_ERROR_RESPONSES)
async def check_now(service_id: int, auth: AdminDep, db: DbDep):
    """Compare Proxmox with the rendered firewall now; writes nothing."""
    service = _service(db, service_id)
    if db.get(ServiceFirewall, service.id) is None:
        raise FirewallNotFound("This VM has no firewall yet", code="firewall_not_found")
    client = await _proxmox(db, service)
    return dataclasses.asdict(await fw_apply.check(db, service.id, client))
