"""Client VM firewall API (docs/vm-firewall.md section 10.1).

Service routes: ``/api/client/services/{service_id}/firewall...``; the service must be owned by
the caller (404 otherwise), viewing needs nothing more, writes need the ``vm.firewall`` client
permission. Account objects: ``/api/client/firewall/groups`` and ``/ipsets`` need a live client
session. Every write answers with the full firewall view (or the object) carrying the new
``revision``. The reseller panel gets a read-only view of its clients' VMs.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api import vm_firewall_common as fw
from app.api.reseller_panel import ResellerDep
from app.core.auth import get_current_user, security
from app.core.client_permissions import PermissionKey
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao.service_dao import ServiceDAO
from app.models.reseller import ServiceBilling
from app.models.service import Service
from app.services.client_permission_resolver import require_client_permission
from app.services.vm_firewall import service as svc

DbDep = Annotated[Session, Depends(get_db)]
AuthDep = Annotated[dict, Depends(get_current_user)]
RevisionQuery = Annotated[int, Query(description="The `revision` as last read")]

router = APIRouter(prefix="/client", tags=["client-vm-firewall"])
reseller_router = APIRouter(prefix="/reseller-panel", tags=["reseller-panel"])

_SERVICE = "/services/{service_id}/firewall"
_NOT_FOUND = "Service not found"


class _Caller:
    """The client of the request: its id and the firewall actor (with ``impersonated_by``)."""

    def __init__(self, request: Request, auth: AuthDep, credentials=Depends(security)):
        user_id = auth.get("user_id")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="A user session is required")
        self.auth = auth
        self.user_id = int(user_id)
        self.actor = fw.client_actor(request, credentials, auth)


class _SessionCaller(_Caller):
    """Group and IP set routes need a live client session, not an API key."""

    def __init__(self, request: Request, auth: AuthDep, credentials=Depends(security)):
        if auth.get("type") == "api_key":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="A client session is required")
        super().__init__(request, auth, credentials)


CallerDep = Annotated[_Caller, Depends()]
SessionDep = Annotated[_SessionCaller, Depends()]


def _owned(db: Session, caller: _Caller, service_id: int, *, write: bool = False) -> Service:
    service = ServiceDAO.get_by_id(db, service_id)
    if service is None or service.owner_user_id != caller.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if write:
        require_client_permission(db, service, PermissionKey.VM_FIREWALL)
    return service


def _done(db: Session, service: Service) -> dict[str, Any]:
    db.commit()
    return fw.firewall_view(db, service.id)


# The VM's firewall


@router.get(_SERVICE, responses=COMMON_ERROR_RESPONSES)
def get_firewall(service_id: int, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id)
    return fw.firewall_view(db, service.id)


@router.put(_SERVICE, responses=COMMON_ERROR_RESPONSES)
async def update_firewall(service_id: int, body: fw.FirewallSettingsUpdate, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id, write=True)
    await fw.update_settings(db, service, caller.actor, body)
    return _done(db, service)


@router.put(_SERVICE + "/rules", responses=COMMON_ERROR_RESPONSES)
def replace_rules(service_id: int, body: fw.RulesReplace, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id, write=True)
    fw.replace_rules(db, service, caller.actor, body)
    return _done(db, service)


@router.post(_SERVICE + "/rules", responses=COMMON_ERROR_RESPONSES)
def add_rule(service_id: int, body: fw.RuleCreate, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id, write=True)
    fw.add_rule(db, service, caller.actor, body)
    return _done(db, service)


@router.post(_SERVICE + "/rules/order", responses=COMMON_ERROR_RESPONSES)
def reorder_rules(service_id: int, body: fw.RulesOrder, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id, write=True)
    fw.reorder_rules(db, service, caller.actor, body)
    return _done(db, service)


@router.put(_SERVICE + "/rules/{rule_id}", responses=COMMON_ERROR_RESPONSES)
def update_rule(service_id: int, rule_id: int, body: fw.RuleUpdate, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id, write=True)
    fw.update_rule(db, service, caller.actor, rule_id, body)
    return _done(db, service)


@router.delete(_SERVICE + "/rules/{rule_id}", responses=COMMON_ERROR_RESPONSES)
def delete_rule(service_id: int, rule_id: int, revision: RevisionQuery, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id, write=True)
    fw.delete_rule(db, service, caller.actor, rule_id, revision)
    return _done(db, service)


@router.post(_SERVICE + "/presets/{preset}", responses=COMMON_ERROR_RESPONSES)
def apply_preset(service_id: int, preset: str, body: fw.RevisionOnly, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id, write=True)
    fw.apply_preset(db, service, caller.actor, preset, body)
    return _done(db, service)


@router.post(_SERVICE + "/groups/{group_id}", responses=COMMON_ERROR_RESPONSES)
def attach_group(service_id: int, group_id: int, body: fw.GroupAttach, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id, write=True)
    fw.attach_group(db, service, caller.actor, group_id, body)
    return _done(db, service)


@router.delete(_SERVICE + "/groups/{group_id}", responses=COMMON_ERROR_RESPONSES)
def detach_group(service_id: int, group_id: int, revision: RevisionQuery, caller: CallerDep, db: DbDep):
    service = _owned(db, caller, service_id, write=True)
    fw.detach_group(db, service, caller.actor, group_id, revision)
    return _done(db, service)


@router.get("/firewall/presets", responses=COMMON_ERROR_RESPONSES)
def list_presets(caller: CallerDep):
    return fw.presets_payload()


# Groups


@router.get("/firewall/groups", responses=COMMON_ERROR_RESPONSES)
def list_groups(caller: SessionDep, db: DbDep):
    return [fw.group_payload(db, group) for group in svc.list_groups(db, caller.actor)]


@router.post("/firewall/groups", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_group(body: fw.GroupCreate, caller: SessionDep, db: DbDep):
    group = svc.create_group(db, caller.actor, name=body.name, description=body.description, rules=body.rules)
    db.commit()
    return fw.group_payload(db, group)


@router.get("/firewall/groups/{group_id}", responses=COMMON_ERROR_RESPONSES)
def get_group(group_id: int, caller: SessionDep, db: DbDep):
    return fw.group_payload(db, _client_group(db, caller, group_id))


@router.put("/firewall/groups/{group_id}", responses=COMMON_ERROR_RESPONSES)
def update_group(group_id: int, body: fw.GroupUpdate, caller: SessionDep, db: DbDep):
    _client_group(db, caller, group_id)
    group = svc.update_group(
        db, caller.actor, group_id, revision=body.revision, **fw.given(body, "name", "description", "rules")
    )
    db.commit()
    return fw.group_payload(db, group)


@router.delete("/firewall/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT,
               responses=COMMON_ERROR_RESPONSES)
def delete_group(
    group_id: int,
    caller: SessionDep,
    db: DbDep,
    revision: Optional[int] = None,
    detach: bool = False,
):
    _client_group(db, caller, group_id)
    svc.delete_group(db, caller.actor, group_id, revision=revision, detach=detach)
    db.commit()


def _client_group(db: Session, caller: _Caller, group_id: int):
    """A group the client owns; admin groups (even selectable ones) are read through the VM view."""
    group = svc.get_group(db, caller.actor, group_id)
    if group.owner_user_id != caller.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return group


# IP sets


@router.get("/firewall/ipsets", responses=COMMON_ERROR_RESPONSES)
def list_ipsets(caller: SessionDep, db: DbDep):
    return [fw.ipset_payload(db, ipset) for ipset in svc.list_ipsets(db, caller.actor)]


@router.post("/firewall/ipsets", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_ipset(body: fw.IpsetCreate, caller: SessionDep, db: DbDep):
    ipset = svc.create_ipset(db, caller.actor, name=body.name, description=body.description, entries=body.entries)
    db.commit()
    return fw.ipset_payload(db, ipset)


@router.get("/firewall/ipsets/{ipset_id}", responses=COMMON_ERROR_RESPONSES)
def get_ipset(ipset_id: int, caller: SessionDep, db: DbDep):
    return fw.ipset_payload(db, _client_ipset(db, caller, ipset_id))


@router.put("/firewall/ipsets/{ipset_id}", responses=COMMON_ERROR_RESPONSES)
def update_ipset(ipset_id: int, body: fw.IpsetUpdate, caller: SessionDep, db: DbDep):
    _client_ipset(db, caller, ipset_id)
    ipset = svc.update_ipset(
        db, caller.actor, ipset_id, revision=body.revision, **fw.given(body, "name", "description", "entries")
    )
    db.commit()
    return fw.ipset_payload(db, ipset)


@router.delete("/firewall/ipsets/{ipset_id}", status_code=status.HTTP_204_NO_CONTENT,
               responses=COMMON_ERROR_RESPONSES)
def delete_ipset(ipset_id: int, caller: SessionDep, db: DbDep, revision: Optional[int] = None):
    _client_ipset(db, caller, ipset_id)
    svc.delete_ipset(db, caller.actor, ipset_id, revision=revision)
    db.commit()


def _client_ipset(db: Session, caller: _Caller, ipset_id: int):
    ipset = svc.get_ipset(db, caller.actor, ipset_id)
    if ipset.owner_user_id != caller.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP set not found")
    return ipset


# Reseller panel: read only, the reseller's own services


@reseller_router.get("/services/{service_id}/firewall", responses=COMMON_ERROR_RESPONSES)
def reseller_get_firewall(service_id: int, reseller: ResellerDep, db: DbDep):
    owned = db.query(ServiceBilling).filter(
        ServiceBilling.reseller_id == reseller.id, ServiceBilling.service_id == service_id
    ).one_or_none()
    if owned is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return fw.firewall_view(db, service_id)
