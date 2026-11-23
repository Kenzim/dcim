"""VM firewall routes of the billing API, for WHMCS (docs/vm-firewall.md section 10.3).

The service must belong to the calling integration (404 otherwise, like every billing service
route) and writes need the owner's ``vm.firewall`` client permission. Request and response
shapes are the client ones; admin-only fields are refused (422).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api import vm_firewall_common as fw
from app.api.billing import _SERVICE_NOT_FOUND_MSG, BillingIntegrationDep, _assert_billing_owned_service
from app.core.client_permissions import PermissionKey
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao.service_dao import ServiceDAO
from app.models.billing_integration import BillingIntegration
from app.models.service import Service
from app.services.client_permission_resolver import require_client_permission

DbDep = Annotated[Session, Depends(get_db)]
RevisionQuery = Annotated[int, Query(description="The `revision` as last read")]

router = APIRouter(prefix="/billing/services/{service_id}/firewall", tags=["billing"])


def _owned(db: Session, integration: BillingIntegration, service_id: int, *, write: bool = False) -> Service:
    service = ServiceDAO.get_by_id(db, service_id)
    if service is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    if write:
        require_client_permission(db, service, PermissionKey.VM_FIREWALL)
    return service


def _done(db: Session, service: Service) -> dict[str, Any]:
    db.commit()
    return fw.firewall_view(db, service.id)


@router.get("", responses=COMMON_ERROR_RESPONSES)
def get_firewall(service_id: int, integration: BillingIntegrationDep, db: DbDep):
    return fw.firewall_view(db, _owned(db, integration, service_id).id)


@router.put("", responses=COMMON_ERROR_RESPONSES)
async def update_firewall(
    service_id: int, body: fw.FirewallSettingsUpdate, request: Request, integration: BillingIntegrationDep, db: DbDep
):
    service = _owned(db, integration, service_id, write=True)
    await fw.update_settings(db, service, fw.billing_actor(request, integration.id, service), body)
    return _done(db, service)


@router.put("/rules", responses=COMMON_ERROR_RESPONSES)
def replace_rules(
    service_id: int, body: fw.RulesReplace, request: Request, integration: BillingIntegrationDep, db: DbDep
):
    service = _owned(db, integration, service_id, write=True)
    fw.replace_rules(db, service, fw.billing_actor(request, integration.id, service), body)
    return _done(db, service)


@router.post("/rules", responses=COMMON_ERROR_RESPONSES)
def add_rule(service_id: int, body: fw.RuleCreate, request: Request, integration: BillingIntegrationDep, db: DbDep):
    service = _owned(db, integration, service_id, write=True)
    fw.add_rule(db, service, fw.billing_actor(request, integration.id, service), body)
    return _done(db, service)


@router.delete("/rules/{rule_id}", responses=COMMON_ERROR_RESPONSES)
def delete_rule(
    service_id: int, rule_id: int, revision: RevisionQuery, request: Request, integration: BillingIntegrationDep,
    db: DbDep,
):
    service = _owned(db, integration, service_id, write=True)
    fw.delete_rule(db, service, fw.billing_actor(request, integration.id, service), rule_id, revision)
    return _done(db, service)


@router.post("/presets/{preset}", responses=COMMON_ERROR_RESPONSES)
def apply_preset(
    service_id: int, preset: str, body: fw.RevisionOnly, request: Request, integration: BillingIntegrationDep,
    db: DbDep,
):
    service = _owned(db, integration, service_id, write=True)
    fw.apply_preset(db, service, fw.billing_actor(request, integration.id, service), preset, body)
    return _done(db, service)


@router.get("/groups", responses=COMMON_ERROR_RESPONSES)
def list_groups(service_id: int, integration: BillingIntegrationDep, db: DbDep):
    """The groups the service's owner may attach (their own and client-selectable admin groups)."""
    return fw.owner_groups(db, _owned(db, integration, service_id))
