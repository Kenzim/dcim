"""Tenant-scoped reseller catalog, provisioning, and lifecycle API."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Optional, Union

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Response,
    status,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.client_permissions import PermissionKey
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.core.reseller_auth import get_reseller_by_api_key
from app.dao.ipam_dao import IPAMDAO
from app.dao.reseller_dao import ResellerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.models.reseller import (
    Invoice,
    Reseller,
    ServiceBillingStatus,
    StockQuotaScope,
)
from app.models.service import Service, ServiceStatus, ServiceType
from app.plugins.base import PowerState
from app.schemas.billing import (
    BillingBareMetalServiceCreate,
    BillingServiceResponse,
    BillingVmServiceCreate,
    PowerAction,
    SuspendAction,
)
from app.services.billing_provisioning_service import (
    ProvisioningActor,
    provision_bare_metal_service,
    provision_vm_service,
)
from app.services.client_permission_resolver import require_client_permission
from app.services.client_portal_service import mint_sso_ticket
from app.services.reseller_billing_service import (
    DeployCreditRequiredError,
    IdempotencyConflictError,
    ProductNotAvailableError,
    QuotaNotAllocatedError,
    ResellerBillingError,
    ResellerBillingService,
)
from app.services.service_lifecycle import (
    ServiceLifecycle,
    ServiceLifecycleError,
)


DbDep = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/reseller", tags=["reseller"])
logger = logging.getLogger(__name__)


class ResellerInvoiceResponse(BaseModel):
    """Invoice charged by Rackflow for a reseller deployment."""

    id: int
    invoice_number: int
    status: str
    purpose: str
    amount_cents: int
    currency: str
    service_id: Optional[int] = None
    paid_at: Optional[datetime] = None
    created_at: datetime


class ResellerDeployResponse(BaseModel):
    """Stable reseller create envelope (the service is intentionally nested)."""

    service: BillingServiceResponse
    invoice: ResellerInvoiceResponse
    idempotent_replay: bool


_DEPLOY_402_RESPONSE = {
    "description": (
        "Rackflow did not provision the service. For insufficient credit, "
        "detail contains code, required_cents, available_cents, "
        "shortfall_cents, and invoice_id. Reuse the same Idempotency-Key "
        "after funding that invoice."
    ),
    "content": {
        "application/json": {
            "example": {
                "detail": {
                    "code": "insufficient_credit",
                    "required_cents": 3000,
                    "available_cents": 500,
                    "shortfall_cents": 2500,
                    "invoice_id": 42,
                }
            }
        }
    },
}


def _credit_required(exc: DeployCreditRequiredError) -> HTTPException:
    detail = {
        "code": (
            "payment_action_required"
            if exc.pending_action
            else "insufficient_credit"
        ),
        "required_cents": exc.required_cents,
        "available_cents": exc.available_cents,
        "shortfall_cents": exc.shortfall_cents,
        "invoice_id": exc.invoice_id,
    }
    if exc.pending_action:
        detail.update(
            {
                "pending_action": True,
                "client_secret": exc.client_secret,
                "payment_id": exc.payment_id,
            }
        )
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=detail,
    )


def _enforce_deploy_billing_state(reseller: Reseller) -> None:
    if not reseller.billing_hold and reseller.cached_balance_cents >= 0:
        return
    code = "billing_hold" if reseller.billing_hold else "negative_balance"
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": code,
            "billing_hold": bool(reseller.billing_hold),
            "billing_hold_reason": reseller.billing_hold_reason,
            "balance_cents": reseller.cached_balance_cents,
            "message": "New deployments are blocked until the account is current",
        },
    )


def _invoice_payload(invoice: Invoice) -> dict:
    return {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "status": invoice.status.value,
        "purpose": invoice.purpose.value,
        "amount_cents": invoice.amount_cents,
        "currency": invoice.currency,
        "service_id": invoice.service_id,
        "paid_at": invoice.paid_at,
        "created_at": invoice.created_at,
    }


def _service_payload(db: Session, service: Service) -> dict:
    from app.api.billing import _billing_service_response

    return _billing_service_response(db, service).model_dump()


def _deploy_payload(
    db: Session,
    service: Service,
    invoice: Invoice,
    *,
    idempotent_replay: bool,
) -> dict:
    return {
        "service": _service_payload(db, service),
        "invoice": _invoice_payload(invoice),
        "idempotent_replay": idempotent_replay,
    }


def _product_payload(db: Session, reseller: Reseller, product, price) -> dict:
    from app.api.billing import _billing_product_catalog_item

    payload = _billing_product_catalog_item(db, product)
    payload.update(
        {
            "setup_cents": price.setup_cents,
            "monthly_cents": price.monthly_cents,
            "currency": price.currency,
            "client_permissions": (
                ResellerBillingService.effective_client_product_permissions(
                    db, reseller.id, product
                )
            ),
        }
    )
    return payload


def _scoped_service(
    db: Session, reseller: Reseller, service_id: int
) -> Service:
    service = ServiceDAO.get_by_id_and_reseller(db, service_id, reseller.id)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found",
        )
    return service


@router.get("/products", responses={**COMMON_ERROR_RESPONSES})
async def list_products(
    service_type: Optional[str] = None,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    wanted = (service_type or "").strip().lower() or None
    return [
        _product_payload(db, reseller, product, price)
        for product, price in ResellerBillingService.list_sellable_products(
            db, reseller
        )
        if (
            wanted is None
            or (
                product.family is not None
                and product.family.service_type == wanted
            )
        )
        if ResellerBillingService.client_product_is_visible(
            db, reseller.id, product.id
        )
    ]


@router.get("/products/{product_code}", responses={**COMMON_ERROR_RESPONSES})
async def get_product(
    product_code: str,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    try:
        product, price = ResellerBillingService.resolve_sellable_product(
            db, reseller, product_code
        )
    except ProductNotAvailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    if not ResellerBillingService.client_product_is_visible(
        db, reseller.id, product.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found"
        )
    return _product_payload(db, reseller, product, price)


def _visible_resource_scope_ids(
    db: Session,
    reseller: Reseller,
    *,
    service_types: set[str],
    scope_type: StockQuotaScope,
) -> Optional[set[int]]:
    """Return explicit resource ids, all (None), or none (empty set).

    A resource-scoped quota narrows the loader. When no resource-scoped quota
    exists, a product-scoped quota for a compatible sellable product permits
    the enabled resource catalog. Product access without any matching quota is
    deliberately not enough because create would be rejected before charging.
    """

    products = [
        product
        for product, _price in ResellerBillingService.list_sellable_products(
            db, reseller
        )
        if product.family is not None
        and product.family.service_type in service_types
        and ResellerBillingService.client_product_is_visible(
            db, reseller.id, product.id
        )
    ]
    if not products:
        return set()

    quotas = ResellerDAO.list_effective_stock_quotas(db, reseller.id)
    resource_ids = {
        int(quota.scope_id)
        for quota in quotas
        if quota.scope_type == scope_type
    }
    if resource_ids:
        return resource_ids

    product_ids = {product.id for product in products}
    has_product_quota = any(
        quota.scope_type == StockQuotaScope.PRODUCT
        and quota.scope_id in product_ids
        for quota in quotas
    )
    return None if has_product_quota else set()


@router.get("/proxmox/clusters", response_model=list[dict], responses={**COMMON_ERROR_RESPONSES})
async def list_proxmox_clusters(
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    """List enabled VM locations visible to this reseller's products/quotas."""

    from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO

    visible_ids = _visible_resource_scope_ids(
        db,
        reseller,
        service_types={ServiceType.VM.value},
        scope_type=StockQuotaScope.PROXMOX_CLUSTER,
    )
    return [
        {
            "id": cluster.id,
            "name": cluster.name,
            "nodes": [
                {"node_name": node.node_name, "enabled": True}
                for node in (cluster.nodes or [])
                if node.enabled
            ],
        }
        for cluster in ProxmoxInventoryDAO.list_clusters(db)
        if cluster.enabled
        and (visible_ids is None or cluster.id in visible_ids)
    ]


@router.get("/server-groups", response_model=list[dict], responses={**COMMON_ERROR_RESPONSES})
async def list_server_groups(
    skip: int = 0,
    limit: int = 100,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    """List bare-metal groups visible to this reseller's products/quotas."""

    visible_ids = _visible_resource_scope_ids(
        db,
        reseller,
        service_types={
            ServiceType.BARE_METAL.value,
            ServiceType.HTTP_PROXY.value,
        },
        scope_type=StockQuotaScope.SERVER_GROUP,
    )
    if visible_ids == set():
        return []
    groups = ServerGroupDAO.get_all(
        db, skip=max(skip, 0), limit=min(max(limit, 1), 500)
    )
    return [
        {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "server_count": len(group.servers or []),
            "enable_os_templates": bool(group.enable_os_templates),
            "permitted_os_templates": list(
                group.permitted_os_templates or []
            ),
        }
        for group in groups
        if visible_ids is None or group.id in visible_ids
    ]


def _create_request_context(kind, body, idempotency_key):
    product_code = (body.product_code or "").strip()
    if not product_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="product_code is required for reseller provisioning",
        )
    key = (idempotency_key or "").strip() or None
    if key and len(key) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be at most 255 characters",
        )
    request_hash = ResellerBillingService.request_hash(
        kind, body.model_dump(mode="json")
    )
    return product_code, key, request_hash


def _idempotent_replay_payload(
    db, reseller, key, request_hash
) -> Optional[dict]:
    try:
        replay = ResellerBillingService.lookup_idempotent_result(
            db,
            reseller=reseller,
            idempotency_key=key,
            request_hash=request_hash,
        )
    except DeployCreditRequiredError as exc:
        raise _credit_required(exc) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    if replay is None:
        return None
    return _deploy_payload(
        db,
        replay.service,
        replay.invoice,
        idempotent_replay=True,
    )


def _resolve_product_for_api(db, reseller, product_code):
    try:
        return ResellerBillingService.resolve_sellable_product(
            db, reseller, product_code
        )
    except ProductNotAvailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


def _validate_create_product_and_identity(
    db, reseller, product, body, kind
):
    expected_type = (
        ServiceType.VM.value
        if kind == ServiceType.VM.value
        else (body.service_type or ServiceType.BARE_METAL.value).lower()
    )
    if product.family.service_type != expected_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Product '{product.code}' belongs to service_type "
                f"'{product.family.service_type}', not '{expected_type}'"
            ),
        )
    if not body.external_service_id:
        return
    duplicate = ServiceDAO.get_by_external_service_id_and_reseller(
        db, body.external_service_id, reseller.id
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="external_service_id is already in use",
        )


def _enforce_create_quotas(db, reseller, product, body, kind):
    service_config = body.service_config or {}
    server_group_id = (
        service_config.get("server_group_id")
        if kind != ServiceType.VM.value
        else None
    )
    proxmox_cluster_id = (
        body.proxmox_cluster_id if kind == ServiceType.VM.value else None
    )
    try:
        ResellerBillingService.enforce_quotas(
            db,
            reseller=reseller,
            product=product,
            server_group_id=server_group_id,
            proxmox_cluster_id=proxmox_cluster_id,
        )
    except QuotaNotAllocatedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except ResellerBillingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


def _prepare_charge(
    db, reseller, product, price, key, request_hash
):
    try:
        return ResellerBillingService.prepare_deploy_charge(
            db,
            reseller=reseller,
            product=product,
            price=price,
            idempotency_key=key,
            request_hash=request_hash,
        )
    except DeployCreditRequiredError as exc:
        raise _credit_required(exc) from exc
    except ResellerBillingError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


def _cleanup_failed_service(db, reseller, owner, body, service):
    if service is None and body.external_service_id:
        service = ServiceDAO.get_by_external_service_id_and_owner(
            db, body.external_service_id, owner.id
        )
    if service is None:
        candidate = ServiceDAO.get_by_name(db, body.name)
        if candidate is not None and candidate.owner_user_id == owner.id:
            service = candidate
    if service is None:
        return
    VMIPAllocationDAO.release_for_service(db, service.id)
    IPAMDAO.release_all_for_service(
        db, service.id, released_by=f"reseller:{reseller.id}"
    )
    ServiceDAO.delete(db, service.id)


async def _provision_charged_service(
    *, db, reseller, owner, body, kind, background_tasks, charge
):
    service = None
    actor = ProvisioningActor(
        kind="reseller",
        actor_id=reseller.id,
        name=str(reseller.id),
        source="reseller_api",
    )
    try:
        if kind == ServiceType.VM.value:
            service = await provision_vm_service(
                db=db,
                body=body,
                owner_user_id=owner.id,
                actor=actor,
                background_tasks=background_tasks,
            )
        else:
            service = await provision_bare_metal_service(
                db=db,
                service_data=body,
                owner_user_id=owner.id,
                actor=actor,
            )
        effective_permissions = (
            ResellerBillingService.effective_client_product_permissions(
                db, reseller.id, charge.product
            )
        )
        # Persist reseller denials only. Positive grants continue to inherit
        # from Rackflow's product policy, so a later product restriction can
        # never be bypassed by a stale service-level ``True`` override.
        service.permission_overrides = {
            key: False
            for key, allowed in effective_permissions.items()
            if not allowed
        }
        ResellerBillingService.finalize_deploy(
            db, reseller=reseller, service=service, charge=charge
        )
        return service
    except Exception as exc:
        ResellerBillingService.compensate_failed_deploy(
            db, reseller=reseller, charge=charge, error=exc
        )
        _cleanup_failed_service(
            db, reseller, owner, body, service
        )
        raise


async def _create_service(
    *,
    kind: str,
    body: Union[BillingBareMetalServiceCreate, BillingVmServiceCreate],
    background_tasks: BackgroundTasks,
    reseller: Reseller,
    db: Session,
    idempotency_key: Optional[str],
) -> dict:
    product_code, key, request_hash = _create_request_context(
        kind, body, idempotency_key
    )
    replay_payload = _idempotent_replay_payload(
        db, reseller, key, request_hash
    )
    if replay_payload is not None:
        return replay_payload
    _enforce_deploy_billing_state(reseller)
    product, price = _resolve_product_for_api(
        db, reseller, product_code
    )
    if not ResellerBillingService.client_product_is_visible(
        db, reseller.id, product.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found"
        )
    _validate_create_product_and_identity(
        db, reseller, product, body, kind
    )

    owner = ResellerBillingService.ensure_client_user(
        db,
        reseller=reseller,
        external_user_id=body.external_user_id,
        external_username=body.external_username,
        external_email=body.external_email,
    )
    _enforce_create_quotas(db, reseller, product, body, kind)
    charge = _prepare_charge(
        db, reseller, product, price, key, request_hash
    )
    service = await _provision_charged_service(
        db=db,
        reseller=reseller,
        owner=owner,
        body=body,
        kind=kind,
        background_tasks=background_tasks,
        charge=charge,
    )
    return _deploy_payload(
        db, service, charge.invoice, idempotent_replay=False
    )


@router.post(
    "/bare-metal/services",
    status_code=status.HTTP_201_CREATED,
    response_model=ResellerDeployResponse,
    responses={402: _DEPLOY_402_RESPONSE},
)
async def create_bare_metal_service(
    body: BillingBareMetalServiceCreate,
    background_tasks: BackgroundTasks,
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key"
    ),
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    return await _create_service(
        kind=(body.service_type or ServiceType.BARE_METAL.value).lower(),
        body=body,
        background_tasks=background_tasks,
        reseller=reseller,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/vm/services",
    status_code=status.HTTP_201_CREATED,
    response_model=ResellerDeployResponse,
    responses={402: _DEPLOY_402_RESPONSE},
)
async def create_vm_service(
    body: BillingVmServiceCreate,
    background_tasks: BackgroundTasks,
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key"
    ),
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    return await _create_service(
        kind=ServiceType.VM.value,
        body=body,
        background_tasks=background_tasks,
        reseller=reseller,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.get("/services", responses={**COMMON_ERROR_RESPONSES})
async def list_services(
    skip: int = 0,
    limit: int = 100,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    return [
        _service_payload(db, service)
        for service in ServiceDAO.list_by_reseller(
            db, reseller.id, skip=skip, limit=min(max(limit, 1), 500)
        )
    ]


@router.get("/services/{service_id}", responses={**COMMON_ERROR_RESPONSES})
async def get_service(
    service_id: int,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    service = _scoped_service(db, reseller, service_id)
    payload = _service_payload(db, service)
    owner = service.owner_user
    payload["external_user"] = (
        {
            "id": owner.id,
            "external_user_id": owner.external_user_id,
            "external_username": owner.external_username,
            "external_email": owner.external_email,
        }
        if owner is not None and owner.reseller_id == reseller.id
        else None
    )
    return payload


@router.delete(
    "/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def terminate_service(
    service_id: int,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    service = _scoped_service(db, reseller, service_id)
    VMIPAllocationDAO.release_for_service(db, service.id)
    IPAMDAO.release_all_for_service(
        db, service.id, released_by=f"reseller:{reseller.id}"
    )
    if service.service_type == ServiceType.VM:
        try:
            from app.services.vm_backup_service import purge_client_backups

            await purge_client_backups(db, service)
        except Exception:
            logger.exception(
                "Failed to purge backups for reseller service %s",
                service.id,
            )
    billing = ResellerDAO.get_service_billing(
        db, reseller.id, service.id
    )
    service.status = ServiceStatus.TERMINATED
    service.terminated_at = datetime.now(timezone.utc)
    if billing is not None:
        billing.status = ServiceBillingStatus.CANCELLED
        billing.next_charge_at = None
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/services/{service_id}/suspend", responses={**COMMON_ERROR_RESPONSES})
async def suspend_service(
    service_id: int,
    action: SuspendAction,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    service = _scoped_service(db, reseller, service_id)
    try:
        await ServiceLifecycle().suspend(
            db, service, reason=action.reason or "reseller_api"
        )
    except ServiceLifecycleError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=str(exc)
        ) from exc
    db.commit()
    return {
        "status": "suspended",
        "message": "Service has been suspended",
        "reason": action.reason,
    }


@router.post("/services/{service_id}/unsuspend", responses={**COMMON_ERROR_RESPONSES})
async def unsuspend_service(
    service_id: int,
    action: SuspendAction,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    service = _scoped_service(db, reseller, service_id)
    await ServiceLifecycle().unsuspend(
        db, service, reason=action.reason or "reseller_api"
    )
    db.commit()
    return {
        "status": "active",
        "message": "Service has been unsuspended",
        "reason": action.reason,
    }


@router.post("/services/{service_id}/power", responses={**COMMON_ERROR_RESPONSES})
async def power_service(
    service_id: int,
    power_action: PowerAction,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    from app.api.billing import _billing_get_plugin_instance
    from app.services.service_resource import service_linked_server

    service = _scoped_service(db, reseller, service_id)
    action = power_action.action.lower()
    if action not in {"on", "off", "reboot", "reset"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Power action must be on, off, reboot, or reset",
        )
    server = service_linked_server(db, service)
    if action in {"on", "reboot", "reset"}:
        if service.status in {
            ServiceStatus.SUSPENDED,
            ServiceStatus.TERMINATED,
        }:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Cannot '{action}' a {service.status.value} service",
            )
        if server is not None and not server.enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Server is administratively disabled",
            )
    plugin, _ = await _billing_get_plugin_instance(db, service)
    try:
        if action == "on":
            success = await plugin.power_on()
        elif action == "off":
            success = await plugin.power_off(force=False)
        else:
            success = await plugin.power_reset()
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server plugin does not support power control",
        ) from exc
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Power action command failed",
        )
    return {"status": "success", "action": action}


@router.get("/services/{service_id}/status", responses={**COMMON_ERROR_RESPONSES})
async def get_service_status(
    service_id: int,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    from app.api.billing import _billing_get_plugin_instance

    service = _scoped_service(db, reseller, service_id)
    power_state = PowerState.UNKNOWN
    power_available = service.service_type != ServiceType.HTTP_PROXY
    if power_available:
        try:
            plugin, _ = await _billing_get_plugin_instance(db, service)
            power_state = await plugin.get_power_state()
        except Exception:
            power_state = PowerState.UNKNOWN
    return {
        **_service_payload(db, service),
        "power_available": power_available,
        "power_state": (
            power_state.value
            if hasattr(power_state, "value")
            else str(power_state)
        ),
    }


@router.post("/services/{service_id}/portal-sso", responses={**COMMON_ERROR_RESPONSES})
async def create_portal_sso(
    service_id: int,
    reseller: Reseller = Depends(get_reseller_by_api_key),
    *,
    db: DbDep,
):
    from app.core.config import settings

    service = _scoped_service(db, reseller, service_id)
    require_client_permission(db, service, PermissionKey.SERVICE_PORTAL)
    if service.owner_user is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Service has no portal user",
        )
    token = mint_sso_ticket(service.owner_user.id)
    return {
        "token": token,
        "redeem_path": "/api/client/sso/redeem",
        "expires_in": settings.client_sso_ticket_ttl_seconds,
    }
