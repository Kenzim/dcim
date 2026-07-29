"""Session-authenticated reseller account, invoice, and payment API."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.client_permissions import PERMISSION_CATALOG
from app.core.auth import require_reseller
from app.core.config import settings
from app.core.database import get_db
from app.core.reseller_auth import issue_reseller_api_key
from app.dao.reseller_dao import ResellerDAO
from app.models.product_catalog import Product
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.reseller import (
    CreditLedgerEntry,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Reseller,
    ResellerChargePreference,
    ResellerClientProductPermission,
    ResellerPaymentMethod,
    ResellerStatus,
    ServiceBilling,
    ServiceBillingStatus,
    StockQuota,
    StockQuotaScope,
)
from app.models.server_group import ServerGroup
from app.models.service import Service, ServiceStatus
from app.models.user import User
from app.models.usdt import UsdtChainCursor, UsdtDeposit, UsdtTransferEvent
from app.services.invoice_service import InvoiceError, InvoiceService
from app.services.payments.base import (
    GatewayNotConfiguredError,
    PaymentGatewayError,
    PaymentMethodOwnershipError,
)
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.payments.registry import payment_gateway_registry
from app.services.payments.webhooks import PaymentWebhookService
from app.services.payments.usdt_crypto import (
    UsdtConfigurationError,
    UsdtDepositError,
    create_or_get_deposit,
    eip681_usdt_uri,
    format_token_units,
)
from app.services.payments.usdt_rpc import EthereumRpcClient, EthereumRpcError
from app.services.reseller_billing_service import (
    ProductNotAvailableError,
    ResellerBillingService,
)


router = APIRouter(prefix="/reseller-panel", tags=["reseller-panel"])
PositiveCents = Annotated[StrictInt, Field(gt=0)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TopupInvoiceCreate(RequestModel):
    amount_cents: PositiveCents


class PaymentMethodRegister(RequestModel):
    payment_method_id: str = Field(min_length=3, max_length=255)
    tier: StrictInt = Field(default=1, ge=0)
    label: Optional[str] = Field(default=None, max_length=255)


class PayPalPaymentMethodRegister(RequestModel):
    setup_token_id: str = Field(min_length=3, max_length=255)
    tier: StrictInt = Field(default=1, ge=0)
    label: Optional[str] = Field(default=None, max_length=255)


class PaymentMethodTier(RequestModel):
    method_id: StrictInt = Field(gt=0)
    tier: StrictInt = Field(ge=0)


class PaymentMethodReorder(RequestModel):
    methods: list[PaymentMethodTier] = Field(min_length=1, max_length=100)


class ChargePreferenceUpdate(RequestModel):
    charge_preference: ResellerChargePreference


class InvoicePay(RequestModel):
    payment_method_id: Optional[StrictInt] = Field(default=None, gt=0)


class ClientProductPermissionsUpdate(RequestModel):
    visible: StrictBool
    permissions: dict[str, StrictBool] = Field(default_factory=dict)


class ApiKeyRotate(RequestModel):
    current_password: str = Field(min_length=1, max_length=255)


def _current_reseller(
    auth: dict = Depends(require_reseller),
    db: Session = Depends(get_db),
) -> Reseller:
    reseller = ResellerDAO.get_by_user_id(db, int(auth["user_id"]))
    if reseller is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reseller account not found",
        )
    if reseller.status != ResellerStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reseller account is not active",
        )
    return reseller


def _invoice_or_404(
    db: Session, reseller: Reseller, invoice_id: int
) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.reseller_id != reseller.id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def _method_payload(method: ResellerPaymentMethod) -> dict:
    return {
        "id": method.id,
        "provider": method.provider,
        "method_type": method.method_type,
        "label": method.label,
        "brand": method.brand,
        "last4": method.last4,
        "tier": method.tier,
        "enabled": method.enabled,
        "is_default": method.is_default,
        "created_at": method.created_at,
        "updated_at": method.updated_at,
    }


def _ledger_payload(entry: CreditLedgerEntry) -> dict:
    return {
        "id": entry.id,
        "entry_type": entry.entry_type.value,
        "amount_cents": entry.amount_cents,
        "balance_after_cents": entry.balance_after_cents,
        "description": entry.description,
        "invoice_id": entry.invoice_id,
        "payment_id": entry.payment_id,
        "service_id": entry.service_id,
        "reference_type": entry.reference_type,
        "reference_id": entry.reference_id,
        "created_at": entry.created_at,
    }


def _sellable_product_or_404(
    db: Session, reseller: Reseller, product_id: int
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        resolved, price = ResellerBillingService.resolve_sellable_product(
            db, reseller, product.code
        )
    except ProductNotAvailableError as exc:
        raise HTTPException(status_code=404, detail="Product not found") from exc
    return resolved, price


def _product_policy_payload(
    db: Session, reseller: Reseller, product: Product, price
) -> dict:
    from app.api.billing import _billing_product_catalog_item

    ceiling = ResellerBillingService.product_permission_ceiling(product)
    policy = ResellerBillingService.get_client_product_policy(
        db, reseller.id, product.id
    )
    applicable_catalog = [
        {
            **entry,
            "rackflow_allowed": bool(ceiling.get(entry["key"], False)),
        }
        for entry in PERMISSION_CATALOG
        if entry["key"] in ceiling
    ]
    return {
        **_billing_product_catalog_item(db, product),
        "setup_cents": price.setup_cents,
        "monthly_cents": price.monthly_cents,
        "currency": price.currency,
        "setup_price_source": price.setup_source,
        "monthly_price_source": price.monthly_source,
        "client_visible": policy is None or bool(policy.visible),
        "client_permissions": (
            ResellerBillingService.effective_client_product_permissions(
                db, reseller.id, product
            )
        ),
        "rackflow_permission_ceiling": ceiling,
        "permission_catalog": applicable_catalog,
    }


def _scope_name(
    db: Session, scope_type: StockQuotaScope, scope_id: int
) -> str:
    model = {
        StockQuotaScope.PRODUCT: Product,
        StockQuotaScope.SERVER_GROUP: ServerGroup,
        StockQuotaScope.PROXMOX_CLUSTER: ProxmoxCluster,
    }[scope_type]
    target = db.get(model, scope_id)
    return target.name if target is not None else f"Deleted target {scope_id}"


def _quota_payload(db: Session, reseller: Reseller, quota: StockQuota) -> dict:
    rows = ResellerDAO.list_scope_service_billings(
        db, reseller.id, quota.scope_type, quota.scope_id
    )
    cpu, ram, disk = ResellerBillingService._used_resources(rows)
    usage = {
        "services": len({row.service_id for row in rows}),
        "cpu_cores": cpu,
        "ram_mb": ram,
        "disk_gb": disk,
    }
    limits = {
        "services": quota.max_services,
        "cpu_cores": quota.max_cpu_cores,
        "ram_mb": quota.max_ram_mb,
        "disk_gb": quota.max_disk_gb,
    }
    remaining = {
        key: None if limit is None else max(0, int(limit) - usage[key])
        for key, limit in limits.items()
    }
    warning = any(
        limit is not None
        and (remaining[key] == 0 or usage[key] / max(int(limit), 1) >= 0.8)
        for key, limit in limits.items()
    )
    return {
        "id": quota.id,
        "source": "reseller" if quota.reseller_id is not None else "group",
        "source_id": quota.reseller_id or quota.group_id,
        "scope_type": quota.scope_type.value,
        "scope_id": quota.scope_id,
        "scope_name": _scope_name(db, quota.scope_type, quota.scope_id),
        "limits": limits,
        "usage": usage,
        "remaining": remaining,
        "warning": warning,
    }


def _client_or_404(
    db: Session, reseller: Reseller, client_id: int
) -> User:
    user = db.get(User, client_id)
    if user is None or user.reseller_id != reseller.id:
        raise HTTPException(status_code=404, detail="Client not found")
    return user


def _client_payload(db: Session, user: User, *, services: bool = False) -> dict:
    owned_services = list(
        db.execute(
            select(Service)
            .where(Service.owner_user_id == user.id)
            .order_by(Service.id)
        ).scalars()
    )
    payload = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "external_user_id": user.external_user_id,
        "external_username": user.external_username,
        "external_email": user.external_email,
        "service_count": len(owned_services),
        "created_at": user.created_at,
    }
    if services:
        payload["services"] = [
            {
                "id": service.id,
                "name": service.name,
                "service_type": service.service_type.value,
                "status": service.status.value,
                "product_code": service.product_code,
                "created_at": service.created_at,
            }
            for service in owned_services
        ]
    return payload


def _service_billing_payload(row: ServiceBilling, *, detail: bool = False) -> dict:
    service = row.service
    owner = service.owner_user if service is not None else None
    cycles = list(row.cycles or [])
    current_cycle = cycles[-1] if cycles else None
    payload = {
        "id": service.id if service is not None else row.service_id,
        "billing_id": row.id,
        "name": service.name if service is not None else None,
        "service_type": (
            service.service_type.value if service is not None else None
        ),
        "service_status": (
            service.status.value if service is not None else None
        ),
        "billing_status": row.status.value,
        "product": (
            {
                "id": row.product.id,
                "code": row.product.code,
                "name": row.product.name,
            }
            if row.product is not None
            else None
        ),
        "product_code": service.product_code if service is not None else None,
        "client": (
            {
                "id": owner.id,
                "username": owner.username,
                "email": owner.email,
            }
            if owner is not None
            else None
        ),
        "setup_price_cents": row.setup_price_cents,
        "monthly_price_cents": row.monthly_price_cents,
        "currency": row.currency,
        "next_charge_at": row.next_charge_at,
        "last_charged_at": row.last_charged_at,
        "current_cycle": (
            {
                "id": current_cycle.id,
                "state": current_cycle.state.value,
                "amount_cents": current_cycle.amount_cents,
                "currency": current_cycle.currency,
                "due_at": current_cycle.due_at,
                "paid_at": current_cycle.paid_at,
            }
            if current_cycle is not None
            else None
        ),
    }
    if detail:
        payload.update(
            {
                "billing_anchor_day": row.billing_anchor_day,
                "failed_attempts": row.failed_attempts,
                "grace_until": row.grace_until,
                "next_retry_at": row.next_retry_at,
                "created_at": row.created_at,
            }
        )
    return payload


def _usdt_rpc() -> EthereumRpcClient:
    return EthereumRpcClient(
        str(settings.usdt_rpc_url),
        timeout_seconds=settings.usdt_rpc_timeout_seconds,
        retries=settings.usdt_rpc_retries,
    )


def _usdt_network_name() -> Optional[str]:
    if settings.usdt_chain_id == 1:
        return "ethereum"
    if settings.usdt_chain_id == 11155111:
        return "ethereum-sepolia"
    return None


def _usdt_deposit_payload(db: Session, deposit: UsdtDeposit) -> dict:
    events = list(
        db.execute(
            select(UsdtTransferEvent)
            .where(UsdtTransferEvent.deposit_id == deposit.id)
            .order_by(
                UsdtTransferEvent.block_number,
                UsdtTransferEvent.log_index,
            )
        ).scalars()
    )
    cursor = db.execute(
        select(UsdtChainCursor).where(
            UsdtChainCursor.chain_id == deposit.chain_id,
            UsdtChainCursor.contract_address == deposit.contract_address,
        )
    ).scalar_one_or_none()
    confirmations = 0
    if deposit.first_block is not None and cursor and cursor.last_safe_block is not None:
        estimated_tip = int(cursor.last_safe_block) + settings.usdt_confirmations
        confirmations = max(0, estimated_tip - int(deposit.first_block) + 1)
    network = "ethereum" if deposit.chain_id == 1 else "ethereum-sepolia"
    return {
        "id": deposit.id,
        "invoice_id": deposit.invoice_id,
        "network": network,
        "chain_id": deposit.chain_id,
        "token_symbol": "USDT",
        "contract_address": deposit.contract_address,
        "deposit_address": deposit.deposit_address,
        "amount": format_token_units(
            int(deposit.expected_token_units), settings.usdt_decimals
        ),
        "received_amount": format_token_units(
            int(deposit.received_token_units), settings.usdt_decimals
        ),
        "token_decimals": settings.usdt_decimals,
        "status": deposit.status.value,
        "expires_at": deposit.expires_at,
        "confirmations": confirmations,
        "confirmations_required": settings.usdt_confirmations,
        "tx_hashes": list(dict.fromkeys(event.tx_hash for event in events)),
        "sweep_tx_hash": deposit.sweep_tx_hash,
        "payment_uri": eip681_usdt_uri(
            deposit.contract_address,
            int(deposit.chain_id),
            deposit.deposit_address,
            int(deposit.expected_token_units),
        ),
        "created_at": deposit.created_at,
        "updated_at": deposit.updated_at,
    }


@router.get("/dashboard")
def dashboard(
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    invoice_count = db.scalar(
        select(func.count(Invoice.id)).where(
            Invoice.reseller_id == reseller.id
        )
    )
    open_count = db.scalar(
        select(func.count(Invoice.id)).where(
            Invoice.reseller_id == reseller.id,
            Invoice.status.in_(
                [
                    InvoiceStatus.OPEN,
                    InvoiceStatus.PENDING_ACTION,
                    InvoiceStatus.OVERDUE,
                ]
            ),
        )
    )
    billings = ResellerDAO.list_service_billings(db, reseller.id)
    active_billings = [
        row
        for row in billings
        if row.status != ServiceBillingStatus.CANCELLED
    ]
    client_count = db.scalar(
        select(func.count(User.id)).where(User.reseller_id == reseller.id)
    )
    quota_rows = [
        _quota_payload(db, reseller, quota)
        for quota in ResellerDAO.list_effective_stock_quotas(db, reseller.id)
    ]
    return {
        "reseller_id": reseller.id,
        "balance_cents": reseller.cached_balance_cents,
        "currency": "USD",
        "charge_preference": reseller.charge_preference.value,
        "nonpayment_policy": reseller.nonpayment_policy.value,
        "billing_hold": reseller.billing_hold,
        "billing_hold_reason": reseller.billing_hold_reason,
        "billing_hold_at": reseller.billing_hold_at,
        "invoice_count": int(invoice_count or 0),
        "open_invoice_count": int(open_count or 0),
        "client_count": int(client_count or 0),
        "service_count": len(active_billings),
        "monthly_cost_cents": sum(
            int(row.monthly_price_cents) for row in active_billings
        ),
        "quota_warning": any(row["warning"] for row in quota_rows),
        "quota_warning_count": sum(row["warning"] for row in quota_rows),
        "stripe_enabled": settings.stripe_enabled,
        "paypal_enabled": settings.paypal_enabled,
        "usdt_enabled": settings.usdt_enabled,
    }


@router.get("/products")
def list_products(
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    return [
        _product_policy_payload(db, reseller, product, price)
        for product, price in ResellerBillingService.list_sellable_products(
            db, reseller
        )
    ]


@router.put("/products/{product_id}/client-permissions")
def update_product_client_permissions(
    product_id: int,
    body: ClientProductPermissionsUpdate,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    product, price = _sellable_product_or_404(
        db, reseller, product_id
    )
    ceiling = ResellerBillingService.product_permission_ceiling(product)
    unknown = sorted(set(body.permissions) - set(ceiling))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown or inapplicable permission key(s): {unknown}",
        )
    excessive = sorted(
        key
        for key, granted in body.permissions.items()
        if granted and not ceiling.get(key, False)
    )
    if excessive:
        raise HTTPException(
            status_code=422,
            detail=(
                "Permissions exceed the Rackflow product policy: "
                f"{excessive}"
            ),
        )
    policy = ResellerBillingService.get_client_product_policy(
        db, reseller.id, product.id
    )
    if policy is None:
        policy = ResellerClientProductPermission(
            reseller_id=reseller.id,
            product_id=product.id,
        )
        db.add(policy)
    policy.visible = body.visible
    policy.permissions = dict(body.permissions)
    db.commit()
    db.refresh(policy)
    return _product_policy_payload(db, reseller, product, price)


@router.get("/quotas")
def list_quotas(
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    return [
        _quota_payload(db, reseller, quota)
        for quota in ResellerDAO.list_effective_stock_quotas(db, reseller.id)
    ]


@router.get("/clients")
def list_clients(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    clients = db.execute(
        select(User)
        .where(User.reseller_id == reseller.id)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars()
    return [_client_payload(db, client) for client in clients]


@router.get("/clients/{client_id}")
def get_client(
    client_id: int,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    return _client_payload(
        db, _client_or_404(db, reseller, client_id), services=True
    )


@router.get("/services")
def list_services(
    billing_status: Optional[ServiceBillingStatus] = Query(
        default=None, alias="status"
    ),
    service_status: Optional[ServiceStatus] = None,
    client_id: Optional[int] = Query(default=None, gt=0),
    product_id: Optional[int] = Query(default=None, gt=0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    stmt = (
        select(ServiceBilling)
        .join(Service, Service.id == ServiceBilling.service_id)
        .where(ServiceBilling.reseller_id == reseller.id)
    )
    if billing_status is not None:
        stmt = stmt.where(ServiceBilling.status == billing_status)
    if service_status is not None:
        stmt = stmt.where(Service.status == service_status)
    if client_id is not None:
        stmt = stmt.where(Service.owner_user_id == client_id)
    if product_id is not None:
        stmt = stmt.where(ServiceBilling.product_id == product_id)
    rows = db.execute(
        stmt.order_by(ServiceBilling.created_at.desc(), ServiceBilling.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars()
    return [_service_billing_payload(row) for row in rows]


@router.get("/services/{service_id}")
def get_service(
    service_id: int,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    row = db.execute(
        select(ServiceBilling).where(
            ServiceBilling.reseller_id == reseller.id,
            ServiceBilling.service_id == service_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return _service_billing_payload(row, detail=True)


@router.get("/api-key")
def get_api_key(
    reseller: Reseller = Depends(_current_reseller),
):
    return {
        "configured": bool(reseller.api_key_hash),
        "prefix": reseller.api_key_prefix,
        "last_used_at": reseller.last_used_at,
    }


@router.post("/api-key/rotate")
def rotate_api_key(
    body: ApiKeyRotate,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    user = reseller.user
    if user is None or not user.has_password:
        raise HTTPException(
            status_code=409,
            detail=(
                "This account has no password. Contact an administrator "
                "to rotate the API key."
            ),
        )
    if not user.verify_password(body.current_password):
        raise HTTPException(
            status_code=403, detail="Current password is incorrect"
        )
    plaintext = issue_reseller_api_key(db, reseller)
    db.commit()
    return {
        "api_key": plaintext,
        "prefix": reseller.api_key_prefix,
        "warning": "Copy this key now. It will not be shown again.",
    }


@router.get("/payment-config")
def payment_config(
    reseller: Reseller = Depends(_current_reseller),
):
    del reseller
    stripe_enabled = bool(
        settings.stripe_enabled and settings.stripe_publishable_key
    )
    return {
        "stripe": {
            "enabled": stripe_enabled,
            "publishable_key": (
                settings.stripe_publishable_key if stripe_enabled else None
            ),
        },
        "paypal": {
            "enabled": settings.paypal_enabled,
            "client_id": (
                settings.paypal_client_id if settings.paypal_enabled else None
            ),
            "environment": settings.paypal_environment,
        },
        "usdt": {
            "enabled": settings.usdt_enabled,
            "network": _usdt_network_name(),
            "chain_id": settings.usdt_chain_id,
            "contract_address": settings.usdt_contract_address,
            "symbol": "USDT",
            "token_decimals": settings.usdt_decimals,
        },
    }


@router.get("/balance")
def balance(reseller: Reseller = Depends(_current_reseller)):
    return {
        "balance_cents": reseller.cached_balance_cents,
        "currency": "USD",
    }


@router.get("/invoices")
def list_invoices(
    invoice_status: Optional[InvoiceStatus] = Query(
        default=None, alias="status"
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    return [
        InvoiceService.serialize(db, invoice)
        for invoice in ResellerDAO.list_invoices(
            db,
            reseller.id,
            status=invoice_status,
            offset=offset,
            limit=limit,
        )
    ]


@router.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: int,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    return InvoiceService.serialize(
        db, _invoice_or_404(db, reseller, invoice_id), payments=True
    )


@router.get("/invoices/{invoice_id}/payments")
def list_invoice_payments(
    invoice_id: int,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    invoice = _invoice_or_404(db, reseller, invoice_id)
    return [
        InvoiceService.serialize_payment(payment)
        for payment in ResellerDAO.list_invoice_payments(db, invoice.id)
    ]


@router.post(
    "/invoices/{invoice_id}/usdt-deposit",
    status_code=status.HTTP_201_CREATED,
)
def create_usdt_deposit(
    invoice_id: int,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    invoice = _invoice_or_404(db, reseller, invoice_id)
    if (
        invoice.status != InvoiceStatus.OPEN
        or invoice.purpose != InvoicePurpose.CREDIT_TOPUP
    ):
        raise HTTPException(
            status_code=409,
            detail="Only open credit top-up invoices accept USDT",
        )
    try:
        with _usdt_rpc() as rpc:
            deposit = create_or_get_deposit(
                db,
                reseller_id=reseller.id,
                invoice_id=invoice.id,
                rpc=rpc,
            )
        db.commit()
        db.refresh(deposit)
        return _usdt_deposit_payload(db, deposit)
    except UsdtConfigurationError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (UsdtDepositError, EthereumRpcError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/invoices/{invoice_id}/usdt-deposit")
def get_usdt_deposit(
    invoice_id: int,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    invoice = _invoice_or_404(db, reseller, invoice_id)
    deposit = db.execute(
        select(UsdtDeposit).where(UsdtDeposit.invoice_id == invoice.id)
    ).scalar_one_or_none()
    if deposit is None:
        raise HTTPException(status_code=404, detail="USDT deposit not found")
    return _usdt_deposit_payload(db, deposit)


@router.get("/ledger")
def list_ledger(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    entries = db.execute(
        select(CreditLedgerEntry)
        .where(CreditLedgerEntry.reseller_id == reseller.id)
        .order_by(
            CreditLedgerEntry.created_at.desc(),
            CreditLedgerEntry.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    ).scalars()
    return [_ledger_payload(entry) for entry in entries]


@router.post("/top-up-invoices", status_code=status.HTTP_201_CREATED)
def create_topup_invoice(
    body: TopupInvoiceCreate,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    if not (
        settings.reseller_topup_min_cents
        <= body.amount_cents
        <= settings.reseller_topup_max_cents
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_topup_amount",
                "min_cents": settings.reseller_topup_min_cents,
                "max_cents": settings.reseller_topup_max_cents,
            },
        )
    invoice = InvoiceService.create_topup(
        db, reseller_id=reseller.id, amount_cents=body.amount_cents
    )
    db.commit()
    db.refresh(invoice)
    return InvoiceService.serialize(db, invoice)


@router.post("/stripe/setup-intent")
def create_setup_intent(
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    try:
        setup_intent = PaymentOrchestrator().create_stripe_setup_intent(
            db, reseller
        )
    except GatewayNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.commit()
    return {"client_secret": setup_intent.client_secret}


@router.post("/paypal/setup-token")
def create_paypal_setup_token(
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    try:
        setup = PaymentOrchestrator().create_paypal_vault_setup(db, reseller)
    except GatewayNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PaymentGatewayError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.commit()
    return {
        "setup_token_id": setup.external_ref,
        "approval_url": setup.approval_url,
    }


@router.get("/payment-methods")
def list_payment_methods(
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    return [
        _method_payload(method)
        for method in ResellerDAO.list_payment_methods(
            db, reseller.id, enabled_only=False
        )
    ]


def _register_method(
    db: Session,
    reseller: Reseller,
    body: PaymentMethodRegister,
) -> ResellerPaymentMethod:
    try:
        method = PaymentOrchestrator().register_stripe_method(
            db,
            reseller,
            method_ref=body.payment_method_id.strip(),
            tier=body.tier,
            label=body.label,
        )
    except PaymentMethodOwnershipError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PaymentGatewayError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return method


@router.post("/payment-methods", status_code=status.HTTP_201_CREATED)
def register_payment_method(
    body: PaymentMethodRegister,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    method = _register_method(db, reseller, body)
    db.commit()
    db.refresh(method)
    return _method_payload(method)


@router.post(
    "/paypal/payment-methods", status_code=status.HTTP_201_CREATED
)
def register_paypal_payment_method(
    body: PayPalPaymentMethodRegister,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    try:
        method = PaymentOrchestrator().complete_paypal_vault_setup(
            db,
            reseller,
            setup_token_ref=body.setup_token_id.strip(),
            tier=body.tier,
            label=body.label,
        )
    except PaymentMethodOwnershipError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PaymentGatewayError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(method)
    return _method_payload(method)


@router.put("/payment-methods/reorder")
def reorder_payment_methods(
    body: PaymentMethodReorder,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    method_ids = [item.method_id for item in body.methods]
    if len(method_ids) != len(set(method_ids)):
        raise HTTPException(status_code=422, detail="Duplicate method_id")
    methods = list(
        db.execute(
            select(ResellerPaymentMethod).where(
                ResellerPaymentMethod.reseller_id == reseller.id,
                ResellerPaymentMethod.id.in_(method_ids),
            )
        ).scalars()
    )
    if len(methods) != len(method_ids):
        raise HTTPException(status_code=404, detail="Payment method not found")
    tiers = {item.method_id: item.tier for item in body.methods}
    for method in methods:
        method.tier = tiers[method.id]
    db.commit()
    return [
        _method_payload(method)
        for method in sorted(methods, key=lambda row: (row.tier, row.id))
    ]


@router.put("/payment-methods/{method_id}")
def sync_payment_method(
    method_id: int,
    body: PaymentMethodRegister,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    existing = db.get(ResellerPaymentMethod, method_id)
    if existing is None or existing.reseller_id != reseller.id:
        raise HTTPException(status_code=404, detail="Payment method not found")
    method = _register_method(db, reseller, body)
    if method.id != existing.id:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="PaymentMethod id does not match this saved method",
        )
    db.commit()
    db.refresh(method)
    return _method_payload(method)


@router.delete(
    "/payment-methods/{method_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_payment_method(
    method_id: int,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    try:
        PaymentOrchestrator().detach_method(db, reseller, method_id)
    except PaymentMethodOwnershipError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PaymentGatewayError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.commit()
    return None


@router.put("/charge-preference")
def update_charge_preference(
    body: ChargePreferenceUpdate,
    reseller: Annotated[Reseller, Depends(_current_reseller)],
    db: Annotated[Session, Depends(get_db)],
):
    reseller.charge_preference = body.charge_preference
    db.commit()
    return {"charge_preference": reseller.charge_preference.value}


@router.post("/invoices/{invoice_id}/pay")
def pay_invoice(
    invoice_id: int,
    body: InvoicePay,
    reseller: Reseller = Depends(_current_reseller),
    db: Session = Depends(get_db),
):
    invoice = _invoice_or_404(db, reseller, invoice_id)
    try:
        result = PaymentOrchestrator().fund_invoice(
            db,
            reseller,
            invoice,
            payment_method_id=body.payment_method_id,
        )
    except PaymentMethodOwnershipError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvoiceError, PaymentGatewayError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(invoice)
    if not result.funded:
        detail = {
            "code": (
                "payment_action_required"
                if result.pending_action
                else "payment_failed"
            ),
            "pending_action": result.pending_action,
            "invoice_id": invoice.id,
            "allocated_cents": result.allocated_cents,
            "remaining_cents": result.remaining_cents,
            "payment_id": result.payment_id,
        }
        if result.pending_action:
            detail["client_secret"] = result.client_secret
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=detail
        )
    return InvoiceService.serialize(db, invoice, payments=True)


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(
        default=None, alias="Stripe-Signature"
    ),
    db: Session = Depends(get_db),
):
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Stripe-Signature is required")
    payload = await request.body()
    try:
        gateway = payment_gateway_registry.get("stripe")
        event = gateway.construct_webhook_event(
            payload=payload, signature=stripe_signature
        )
        normalized = gateway.normalize_webhook_event(event)
    except GatewayNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail="Invalid Stripe webhook signature"
        ) from exc
    try:
        processed = PaymentWebhookService.process(
            db,
            gateway="stripe",
            event=normalized,
            raw_payload=payload,
        )
        db.commit()
    except PaymentGatewayError as exc:
        db.rollback()
        raise HTTPException(
            status_code=400, detail="Invalid Stripe webhook event"
        ) from exc
    except Exception:
        db.rollback()
        raise
    return {"received": True, "duplicate": not processed}


@router.post("/webhooks/paypal")
async def paypal_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    payload = await request.body()
    try:
        gateway = payment_gateway_registry.get("paypal")
        event = gateway.construct_webhook_event(
            payload=payload,
            headers=dict(request.headers),
        )
        normalized = gateway.normalize_webhook_event(event)
    except GatewayNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PaymentGatewayError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid PayPal webhook signature"
        ) from exc
    try:
        processed = PaymentWebhookService.process(
            db,
            gateway="paypal",
            event=normalized,
            raw_payload=payload,
        )
        db.commit()
    except PaymentGatewayError as exc:
        db.rollback()
        raise HTTPException(
            status_code=400, detail="Invalid PayPal webhook event"
        ) from exc
    except Exception:
        db.rollback()
        raise
    return {"received": True, "duplicate": not processed}
