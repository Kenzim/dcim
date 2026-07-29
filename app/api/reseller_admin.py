"""Administrative API for reseller accounts, pricing, access, and credit."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.auth import require_admin
from app.core.config import settings
from app.core.database import get_db
from app.core.reseller_auth import issue_reseller_api_key
from app.dao.reseller_dao import ResellerDAO
from app.models.product_catalog import Product
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    CreditLedgerEntry,
    Invoice,
    InvoicePurpose,
    NotificationOutbox,
    NotificationOutboxStatus,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    ProductPrice,
    Reseller,
    ResellerChargePreference,
    ResellerGroup,
    ResellerNonpaymentPolicy,
    ResellerGroupPrice,
    ResellerProductAccess,
    ResellerStatus,
    ServiceBilling,
    ServiceBillingStatus,
    StockQuota,
    StockQuotaScope,
)
from app.models.server_group import ServerGroup
from app.models.service import Service
from app.models.user import User
from app.models.usdt import (
    UsdtChainCursor,
    UsdtDeposit,
    UsdtDepositStatus,
    UsdtTransferEvent,
)
from app.services.credit_ledger_service import (
    CreditLedgerError,
    CreditLedgerService,
    InsufficientCreditError,
)
from app.services.invoice_service import InvoiceService
from app.services.reseller_billing_service import ResellerBillingService
from app.services.recurring_billing_service import RecurringBillingService
from app.services.payments.refunds import PaymentRefundError, PaymentRefundService
from app.services.payments.usdt_crypto import format_token_units
from app.services.payments.usdt_rpc import EthereumRpcClient, EthereumRpcError
from app.services.payments.usdt_watcher import UsdtWatcher


router = APIRouter(
    prefix="/admin",
    tags=["reseller-admin"],
    dependencies=[Depends(require_admin)],
)

NonNegativeCents = Annotated[StrictInt, Field(ge=0)]
NullableNonNegativeCents = Annotated[Optional[StrictInt], Field(ge=0)]
NullableNonNegativeInt = Annotated[Optional[StrictInt], Field(ge=0)]
_NOT_BLANK = "must not be blank"
_USDT_DEPOSIT_NOT_FOUND = "USDT deposit not found"


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResellerGroupCreate(RequestModel):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=65535)
    enabled: StrictBool = True

    @field_validator("name", "code")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(_NOT_BLANK)
        return value


class ResellerGroupUpdate(RequestModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=65535)
    enabled: Optional[StrictBool] = None

    @field_validator("name", "code")
    @classmethod
    def strip_optional(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError(_NOT_BLANK)
        return value


class ResellerCreate(RequestModel):
    username: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    group_id: Optional[StrictInt] = Field(default=None, gt=0)
    status: ResellerStatus = ResellerStatus.ACTIVE
    charge_preference: ResellerChargePreference = (
        ResellerChargePreference.CREDIT_FIRST
    )
    nonpayment_policy: ResellerNonpaymentPolicy = (
        ResellerNonpaymentPolicy.BLOCK_NEW
    )

    @field_validator("username")
    @classmethod
    def strip_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(_NOT_BLANK)
        return value


class ResellerUpdate(RequestModel):
    username: Optional[str] = Field(default=None, min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    group_id: Optional[StrictInt] = Field(default=None, gt=0)
    status: Optional[ResellerStatus] = None
    charge_preference: Optional[ResellerChargePreference] = None
    nonpayment_policy: Optional[ResellerNonpaymentPolicy] = None
    billing_hold: Optional[StrictBool] = None
    billing_hold_reason: Optional[str] = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_billing_hold_reason(self):
        if self.billing_hold is True and not (self.billing_hold_reason or "").strip():
            raise ValueError("billing_hold_reason is required when enabling a hold")
        return self

    @field_validator("username")
    @classmethod
    def strip_optional_username(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError(_NOT_BLANK)
        return value


class BasePriceUpsert(RequestModel):
    setup_cents: NonNegativeCents
    monthly_cents: NonNegativeCents
    currency: Literal["USD"] = "USD"


class GroupPriceUpsert(RequestModel):
    setup_cents: NullableNonNegativeCents = None
    monthly_cents: NullableNonNegativeCents = None


class ProductAccessUpsert(RequestModel):
    allowed: StrictBool


class StockQuotaCreate(RequestModel):
    reseller_id: Optional[StrictInt] = Field(default=None, gt=0)
    group_id: Optional[StrictInt] = Field(default=None, gt=0)
    scope_type: StockQuotaScope
    scope_id: StrictInt = Field(gt=0)
    max_services: NullableNonNegativeInt = None
    max_cpu_cores: NullableNonNegativeInt = None
    max_ram_mb: NullableNonNegativeInt = None
    max_disk_gb: NullableNonNegativeInt = None
    enabled: StrictBool = True

    @model_validator(mode="after")
    def validate_owner_and_limits(self):
        if (self.reseller_id is None) == (self.group_id is None):
            raise ValueError("exactly one of reseller_id or group_id is required")
        if all(
            value is None
            for value in (
                self.max_services,
                self.max_cpu_cores,
                self.max_ram_mb,
                self.max_disk_gb,
            )
        ):
            raise ValueError("at least one quota limit is required")
        return self


class StockQuotaUpdate(RequestModel):
    max_services: NullableNonNegativeInt = None
    max_cpu_cores: NullableNonNegativeInt = None
    max_ram_mb: NullableNonNegativeInt = None
    max_disk_gb: NullableNonNegativeInt = None
    enabled: Optional[StrictBool] = None


class CreditAdjustmentCreate(RequestModel):
    amount_cents: StrictInt
    reason: str = Field(min_length=1, max_length=512)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=255)

    @field_validator("amount_cents")
    @classmethod
    def nonzero_amount(cls, value: int) -> int:
        if value == 0:
            raise ValueError("amount_cents must not be zero")
        return value


class UsdtRescanRequest(RequestModel):
    from_block: Optional[StrictInt] = Field(default=None, ge=0)


class UsdtManualReviewRequest(RequestModel):
    reason: Optional[str] = Field(default=None, max_length=512)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


def _enum_value(value):
    return value.value if hasattr(value, "value") else value


def _group_or_404(db: Session, group_id: int) -> ResellerGroup:
    group = db.get(ResellerGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Reseller group not found")
    return group


def _reseller_or_404(db: Session, reseller_id: int) -> Reseller:
    reseller = db.get(Reseller, reseller_id)
    if reseller is None:
        raise HTTPException(status_code=404, detail="Reseller not found")
    return reseller


def _product_or_404(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


def _counts_for_group(db: Session, group_id: int) -> dict:
    return {
        "member_count": db.scalar(
            select(func.count(Reseller.id)).where(Reseller.group_id == group_id)
        )
        or 0,
        "product_price_count": db.scalar(
            select(func.count(ResellerGroupPrice.id)).where(
                ResellerGroupPrice.group_id == group_id
            )
        )
        or 0,
        "product_access_count": db.scalar(
            select(func.count(ResellerProductAccess.id)).where(
                ResellerProductAccess.group_id == group_id
            )
        )
        or 0,
    }


def _group_payload(db: Session, group: ResellerGroup) -> dict:
    return {
        "id": group.id,
        "name": group.name,
        "code": group.code,
        "description": group.description,
        "enabled": group.enabled,
        **_counts_for_group(db, group.id),
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def _invoice_summary(db: Session, reseller_id: int) -> dict:
    rows = list(
        db.execute(
            select(Invoice).where(Invoice.reseller_id == reseller_id)
        ).scalars()
    )
    return {
        "total": len(rows),
        "open": sum(
            row.status in {InvoiceStatus.OPEN, InvoiceStatus.OVERDUE}
            for row in rows
        ),
        "paid": sum(row.status == InvoiceStatus.PAID for row in rows),
        "paid_total_cents": sum(
            row.amount_cents for row in rows if row.status == InvoiceStatus.PAID
        ),
    }


def _payment_summary(db: Session, reseller_id: int) -> dict:
    rows = list(
        db.execute(
            select(Payment)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(Invoice.reseller_id == reseller_id)
        ).scalars()
    )
    return {
        "total": len(rows),
        "succeeded": sum(_enum_value(row.status) == "succeeded" for row in rows),
        "succeeded_total_cents": sum(
            row.amount_cents
            for row in rows
            if _enum_value(row.status) == "succeeded"
        ),
    }


def _service_summary(db: Session, reseller_id: int) -> dict:
    rows = list(
        db.execute(
            select(ServiceBilling).where(
                ServiceBilling.reseller_id == reseller_id
            )
        ).scalars()
    )
    by_status: dict[str, int] = {}
    for row in rows:
        key = _enum_value(row.status)
        by_status[key] = by_status.get(key, 0) + 1
    return {"total": len(rows), "by_status": by_status}


def _reseller_payload(
    db: Session, reseller: Reseller, *, include_details: bool = False
) -> dict:
    user = reseller.user
    result = {
        "id": reseller.id,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_reseller": user.is_reseller,
        },
        "group": (
            {
                "id": reseller.group.id,
                "name": reseller.group.name,
                "code": reseller.group.code,
                "enabled": reseller.group.enabled,
            }
            if reseller.group
            else None
        ),
        "group_id": reseller.group_id,
        "status": _enum_value(reseller.status),
        "charge_preference": _enum_value(reseller.charge_preference),
        "nonpayment_policy": _enum_value(reseller.nonpayment_policy),
        "billing_hold": reseller.billing_hold,
        "billing_hold_at": reseller.billing_hold_at,
        "billing_hold_cleared_at": reseller.billing_hold_cleared_at,
        "billing_hold_reason": reseller.billing_hold_reason,
        "balance_cents": reseller.cached_balance_cents,
        "api_key_prefix": reseller.api_key_prefix,
        "last_used_at": reseller.last_used_at,
        "last_used_ip": reseller.last_used_ip,
        "created_at": reseller.created_at,
        "updated_at": reseller.updated_at,
    }
    if include_details:
        result.update(
            {
                "invoice_summary": _invoice_summary(db, reseller.id),
                "payment_summary": _payment_summary(db, reseller.id),
                "service_summary": _service_summary(db, reseller.id),
                "quota_usage": _list_quota_payloads(
                    db, reseller_id=reseller.id, effective_only=True
                ),
            }
        )
    return result


def _product_payload(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "code": product.code,
        "enabled": product.enabled,
        "family": (
            {
                "id": product.family.id,
                "name": product.family.name,
                "code": product.family.code,
                "service_type": product.family.service_type,
            }
            if product.family
            else None
        ),
    }


def _base_price_payload(product: Product, price: Optional[ProductPrice]) -> dict:
    return {
        "product": _product_payload(product),
        "product_id": product.id,
        "setup_cents": price.setup_cents if price else None,
        "monthly_cents": price.monthly_cents if price else None,
        "currency": price.currency if price else "USD",
        "configured": price is not None,
    }


def _group_price_payload(
    product: Product,
    base: Optional[ProductPrice],
    override: Optional[ResellerGroupPrice],
) -> dict:
    effective_setup_cents = base.setup_cents if base else None
    effective_monthly_cents = base.monthly_cents if base else None
    if override is not None:
        if override.setup_cents is not None:
            effective_setup_cents = override.setup_cents
        if override.monthly_cents is not None:
            effective_monthly_cents = override.monthly_cents
    return {
        "product": _product_payload(product),
        "product_id": product.id,
        "base_setup_cents": base.setup_cents if base else None,
        "base_monthly_cents": base.monthly_cents if base else None,
        "currency": base.currency if base else "USD",
        "override_setup_cents": override.setup_cents if override else None,
        "override_monthly_cents": override.monthly_cents if override else None,
        "effective_setup_cents": effective_setup_cents,
        "effective_monthly_cents": effective_monthly_cents,
        "has_override": override is not None,
    }


def _scope_name(db: Session, scope_type: StockQuotaScope, scope_id: int) -> str:
    model = {
        StockQuotaScope.PRODUCT: Product,
        StockQuotaScope.SERVER_GROUP: ServerGroup,
        StockQuotaScope.PROXMOX_CLUSTER: ProxmoxCluster,
    }[scope_type]
    target = db.get(model, scope_id)
    return target.name if target is not None else f"Deleted target {scope_id}"


def _validate_quota_scope(
    db: Session, scope_type: StockQuotaScope, scope_id: int
) -> None:
    model = {
        StockQuotaScope.PRODUCT: Product,
        StockQuotaScope.SERVER_GROUP: ServerGroup,
        StockQuotaScope.PROXMOX_CLUSTER: ProxmoxCluster,
    }[scope_type]
    if db.get(model, scope_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"{scope_type.value.replace('_', ' ').title()} not found",
        )


def _usage_for_reseller(
    db: Session, reseller_id: int, scope_type: StockQuotaScope, scope_id: int
) -> dict:
    rows = ResellerDAO.list_scope_service_billings(
        db, reseller_id, scope_type, scope_id
    )
    cpu, ram, disk = ResellerBillingService._used_resources(rows)
    return {
        "services": len({row.service_id for row in rows}),
        "cpu_cores": cpu,
        "ram_mb": ram,
        "disk_gb": disk,
    }


def _quota_payload(
    db: Session,
    quota: StockQuota,
    *,
    context_reseller_id: Optional[int] = None,
    effective_ids: Optional[set[int]] = None,
) -> dict:
    owner_type = "reseller" if quota.reseller_id is not None else "group"
    if context_reseller_id is not None:
        reseller_ids = [context_reseller_id]
    elif quota.reseller_id is not None:
        reseller_ids = [quota.reseller_id]
    else:
        reseller_ids = list(
            db.execute(
                select(Reseller.id).where(Reseller.group_id == quota.group_id)
            ).scalars()
        )
    usage_by_reseller = {
        reseller_id: _usage_for_reseller(
            db, reseller_id, quota.scope_type, quota.scope_id
        )
        for reseller_id in reseller_ids
        if reseller_id is not None
    }
    usage = {
        key: sum(values[key] for values in usage_by_reseller.values())
        for key in ("services", "cpu_cores", "ram_mb", "disk_gb")
    }
    return {
        "id": quota.id,
        "reseller_id": quota.reseller_id,
        "group_id": quota.group_id,
        "owner_type": owner_type,
        "source": "direct" if owner_type == "reseller" else "group",
        "scope_type": _enum_value(quota.scope_type),
        "scope_id": quota.scope_id,
        "scope_name": _scope_name(db, quota.scope_type, quota.scope_id),
        "max_services": quota.max_services,
        "max_cpu_cores": quota.max_cpu_cores,
        "max_ram_mb": quota.max_ram_mb,
        "max_disk_gb": quota.max_disk_gb,
        "enabled": quota.enabled,
        "usage": usage,
        "usage_by_reseller": usage_by_reseller,
        "is_effective": (
            quota.id in effective_ids if effective_ids is not None else True
        ),
        "created_at": quota.created_at,
        "updated_at": quota.updated_at,
    }


def _list_quota_payloads(
    db: Session,
    *,
    reseller_id: Optional[int] = None,
    group_id: Optional[int] = None,
    effective_only: bool = False,
) -> list[dict]:
    stmt = select(StockQuota)
    context_reseller_id = None
    effective_ids = None
    if reseller_id is not None:
        reseller = _reseller_or_404(db, reseller_id)
        context_reseller_id = reseller.id
        owner_filters = [StockQuota.reseller_id == reseller.id]
        if reseller.group_id is not None:
            owner_filters.append(StockQuota.group_id == reseller.group_id)
        stmt = stmt.where(or_(*owner_filters))
        effective_ids = {
            quota.id
            for quota in ResellerDAO.list_effective_stock_quotas(db, reseller.id)
        }
    elif group_id is not None:
        _group_or_404(db, group_id)
        stmt = stmt.where(StockQuota.group_id == group_id)
    rows = list(
        db.execute(
            stmt.order_by(StockQuota.scope_type, StockQuota.scope_id, StockQuota.id)
        ).scalars()
    )
    if effective_only and effective_ids is not None:
        rows = [row for row in rows if row.id in effective_ids]
    return [
        _quota_payload(
            db,
            row,
            context_reseller_id=context_reseller_id,
            effective_ids=effective_ids,
        )
        for row in rows
    ]


def _ledger_payload(entry: CreditLedgerEntry) -> dict:
    return {
        "id": entry.id,
        "reseller_id": entry.reseller_id,
        "entry_type": _enum_value(entry.entry_type),
        "amount_cents": entry.amount_cents,
        "balance_after_cents": entry.balance_after_cents,
        "description": entry.description,
        "invoice_id": entry.invoice_id,
        "payment_id": entry.payment_id,
        "service_id": entry.service_id,
        "reference_type": entry.reference_type,
        "reference_id": entry.reference_id,
        "idempotency_key": entry.idempotency_key,
        "metadata": entry.ledger_metadata or {},
        "created_by_user_id": entry.created_by_user_id,
        "created_at": entry.created_at,
    }


def _service_brief(service: Optional[Service]) -> Optional[dict]:
    if service is None:
        return None
    return {
        "id": service.id,
        "name": service.name,
        "service_type": _enum_value(service.service_type),
        "status": _enum_value(service.status),
    }


def _reseller_brief(reseller: Optional[Reseller]) -> Optional[dict]:
    if reseller is None:
        return None
    user = reseller.user
    return {
        "id": reseller.id,
        "username": user.username if user is not None else None,
        "email": user.email if user is not None else None,
        "status": _enum_value(reseller.status),
    }


def _payment_payload(payment: Payment) -> dict:
    invoice = payment.invoice
    refundable, refund_block_reason = PaymentRefundService.refundability(payment)
    return {
        "id": payment.id,
        "invoice_id": payment.invoice_id,
        "invoice_number": invoice.invoice_number if invoice is not None else None,
        "invoice_purpose": (
            _enum_value(invoice.purpose) if invoice is not None else None
        ),
        "reseller_id": invoice.reseller_id if invoice is not None else None,
        "service_id": invoice.service_id if invoice is not None else None,
        "gateway": payment.gateway,
        "status": _enum_value(payment.status),
        "amount_cents": payment.amount_cents,
        "currency": payment.currency,
        "external_ref": payment.external_ref,
        "failure_code": payment.failure_code,
        "failure_message": payment.failure_message,
        "metadata": payment.payment_metadata or {},
        "processed_at": payment.processed_at,
        "refunded_at": payment.refunded_at,
        "refundable": refundable,
        "refund_block_reason": refund_block_reason,
        "created_at": payment.created_at,
        "updated_at": payment.updated_at,
    }


def _invoice_payload(
    db: Session,
    invoice: Invoice,
    *,
    include_payments: bool = False,
) -> dict:
    allocated = InvoiceService.allocated_cents(db, invoice.id)
    result = {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "reseller_id": invoice.reseller_id,
        "reseller": _reseller_brief(invoice.reseller),
        "service_id": invoice.service_id,
        "service": _service_brief(invoice.service),
        "purpose": _enum_value(invoice.purpose),
        "status": _enum_value(invoice.status),
        "amount_cents": invoice.amount_cents,
        "allocated_cents": allocated,
        "remaining_cents": max(0, invoice.amount_cents - allocated),
        "currency": invoice.currency,
        "description": invoice.description,
        "due_at": invoice.due_at,
        "paid_at": invoice.paid_at,
        "created_at": invoice.created_at,
        "updated_at": invoice.updated_at,
    }
    if include_payments:
        result["payments"] = [
            _payment_payload(row)
            for row in sorted(invoice.payments, key=lambda item: item.id)
        ]
    return result


def _billing_cycle_payload(cycle: BillingCycle) -> dict:
    billing = cycle.service_billing
    return {
        "id": cycle.id,
        "service_billing_id": cycle.service_billing_id,
        "service_id": billing.service_id,
        "reseller_id": billing.reseller_id,
        "invoice_id": cycle.invoice_id,
        "due_at": cycle.due_at,
        "amount_cents": cycle.amount_cents,
        "currency": cycle.currency,
        "state": _enum_value(cycle.state),
        "attempts": cycle.attempts,
        "last_attempt_at": cycle.last_attempt_at,
        "next_retry_at": cycle.next_retry_at,
        "grace_until": cycle.grace_until,
        "failure_code": cycle.failure_code,
        "failure_message": cycle.failure_message,
        "paid_at": cycle.paid_at,
        "completed_at": cycle.completed_at,
        "created_at": cycle.created_at,
        "updated_at": cycle.updated_at,
    }


def _outbox_payload(row: NotificationOutbox) -> dict:
    return {
        "id": row.id,
        "idempotency_key": row.idempotency_key,
        "reseller_id": row.reseller_id,
        "recipient": row.recipient,
        "event": row.event,
        "template": row.template,
        "data": row.data or {},
        "status": _enum_value(row.status),
        "attempts": row.attempts,
        "next_attempt_at": row.next_attempt_at,
        "error": row.error,
        "sent_at": row.sent_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _admin_usdt_payload(
    db: Session,
    deposit: UsdtDeposit,
    *,
    include_events: bool = False,
) -> dict:
    result = {
        "id": deposit.id,
        "invoice_id": deposit.invoice_id,
        "reseller_id": deposit.reseller_id,
        "chain_id": deposit.chain_id,
        "contract_address": deposit.contract_address,
        "deposit_address": deposit.deposit_address,
        "expected_amount": format_token_units(
            int(deposit.expected_token_units), settings.usdt_decimals
        ),
        "received_amount": format_token_units(
            int(deposit.received_token_units), settings.usdt_decimals
        ),
        "status": _enum_value(deposit.status),
        "first_block": deposit.first_block,
        "confirmed_block": deposit.confirmed_block,
        "block_hash": deposit.block_hash,
        "credited_at": deposit.credited_at,
        "expires_at": deposit.expires_at,
        "gas_funding_tx_hash": deposit.gas_funding_tx_hash,
        "sweep_tx_hash": deposit.sweep_tx_hash,
        "sweep_error": deposit.sweep_error,
        "sweep_attempts": deposit.sweep_attempts,
        "created_at": deposit.created_at,
        "updated_at": deposit.updated_at,
    }
    if include_events:
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
        result["events"] = [
            {
                "id": event.id,
                "tx_hash": event.tx_hash,
                "log_index": event.log_index,
                "block_number": event.block_number,
                "block_hash": event.block_hash,
                "from_address": event.from_address,
                "to_address": event.to_address,
                "token_units": str(event.token_units),
                "observed_at": event.observed_at,
                "confirmed_at": event.confirmed_at,
                "reversed_at": event.reversed_at,
            }
            for event in events
        ]
    return result


def _usdt_deposit_or_404(db: Session, deposit_id: int) -> UsdtDeposit:
    deposit = db.get(UsdtDeposit, deposit_id)
    if deposit is None:
        raise HTTPException(status_code=404, detail=_USDT_DEPOSIT_NOT_FOUND)
    return deposit


def _admin_usdt_rpc() -> EthereumRpcClient:
    return EthereumRpcClient(
        str(settings.usdt_rpc_url),
        timeout_seconds=settings.usdt_rpc_timeout_seconds,
        retries=settings.usdt_rpc_retries,
    )


@router.get("/reseller-groups")
async def list_reseller_groups(db: Session = Depends(get_db)):
    groups = list(
        db.execute(select(ResellerGroup).order_by(ResellerGroup.name)).scalars()
    )
    return [_group_payload(db, group) for group in groups]


@router.post("/reseller-groups", status_code=status.HTTP_201_CREATED)
async def create_reseller_group(
    body: ResellerGroupCreate, db: Session = Depends(get_db)
):
    duplicate = db.execute(
        select(ResellerGroup).where(
            or_(
                func.lower(ResellerGroup.name) == body.name.lower(),
                func.lower(ResellerGroup.code) == body.code.lower(),
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(
            status_code=409, detail="Reseller group name or code is already in use"
        )
    group = ResellerGroup(**body.model_dump())
    db.add(group)
    db.commit()
    db.refresh(group)
    return _group_payload(db, group)


@router.get("/reseller-groups/{group_id}")
async def get_reseller_group(group_id: int, db: Session = Depends(get_db)):
    return _group_payload(db, _group_or_404(db, group_id))


@router.put("/reseller-groups/{group_id}")
async def update_reseller_group(
    group_id: int,
    body: ResellerGroupUpdate,
    db: Session = Depends(get_db),
):
    group = _group_or_404(db, group_id)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes or "code" in changes:
        name = changes.get("name", group.name)
        code = changes.get("code", group.code)
        duplicate = db.execute(
            select(ResellerGroup).where(
                ResellerGroup.id != group.id,
                or_(
                    func.lower(ResellerGroup.name) == name.lower(),
                    func.lower(ResellerGroup.code) == code.lower(),
                ),
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail="Reseller group name or code is already in use",
            )
    for key, value in changes.items():
        setattr(group, key, value)
    db.commit()
    db.refresh(group)
    return _group_payload(db, group)


@router.delete("/reseller-groups/{group_id}", status_code=204)
async def delete_reseller_group(
    group_id: int, db: Session = Depends(get_db)
):
    group = _group_or_404(db, group_id)
    if _counts_for_group(db, group.id)["member_count"]:
        raise HTTPException(
            status_code=409,
            detail=(
                "Reseller group is in use; reassign its members or disable the "
                "group instead"
            ),
        )
    db.delete(group)
    db.commit()
    return Response(status_code=204)


# Static reseller collection routes are intentionally declared before
# /resellers/{reseller_id}, so their names cannot be consumed as an id.
@router.get("/resellers/prices")
async def list_base_product_prices(db: Session = Depends(get_db)):
    products = list(db.execute(select(Product).order_by(Product.name)).scalars())
    prices = {
        row.product_id: row
        for row in db.execute(select(ProductPrice)).scalars()
    }
    return [_base_price_payload(product, prices.get(product.id)) for product in products]


@router.put("/resellers/prices/{product_id}")
async def upsert_base_product_price(
    product_id: int,
    body: BasePriceUpsert,
    db: Session = Depends(get_db),
):
    product = _product_or_404(db, product_id)
    price = db.execute(
        select(ProductPrice).where(ProductPrice.product_id == product.id)
    ).scalar_one_or_none()
    if price is None:
        price = ProductPrice(product_id=product.id)
        db.add(price)
    price.setup_cents = body.setup_cents
    price.monthly_cents = body.monthly_cents
    price.currency = body.currency
    db.commit()
    db.refresh(price)
    return _base_price_payload(product, price)


@router.get("/resellers/quotas")
async def list_stock_quotas(
    reseller_id: Optional[int] = None,
    group_id: Optional[int] = None,
    effective_only: bool = False,
    db: Session = Depends(get_db),
):
    if reseller_id is not None and group_id is not None:
        raise HTTPException(
            status_code=400, detail="Filter by reseller_id or group_id, not both"
        )
    return _list_quota_payloads(
        db,
        reseller_id=reseller_id,
        group_id=group_id,
        effective_only=effective_only,
    )


@router.post("/resellers/quotas", status_code=status.HTTP_201_CREATED)
async def create_stock_quota(
    body: StockQuotaCreate, db: Session = Depends(get_db)
):
    if body.reseller_id is not None:
        _reseller_or_404(db, body.reseller_id)
    else:
        _group_or_404(db, body.group_id)
    _validate_quota_scope(db, body.scope_type, body.scope_id)
    duplicate = db.execute(
        select(StockQuota).where(
            StockQuota.reseller_id == body.reseller_id,
            StockQuota.group_id == body.group_id,
            StockQuota.scope_type == body.scope_type,
            StockQuota.scope_id == body.scope_id,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(
            status_code=409, detail="A quota already exists for this owner and scope"
        )
    quota = StockQuota(**body.model_dump())
    db.add(quota)
    db.commit()
    db.refresh(quota)
    return _quota_payload(db, quota)


@router.put("/resellers/quotas/{quota_id}")
async def update_stock_quota(
    quota_id: int,
    body: StockQuotaUpdate,
    db: Session = Depends(get_db),
):
    quota = db.get(StockQuota, quota_id)
    if quota is None:
        raise HTTPException(status_code=404, detail="Stock quota not found")
    changes = body.model_dump(exclude_unset=True)
    values = {
        name: changes.get(name, getattr(quota, name))
        for name in (
            "max_services",
            "max_cpu_cores",
            "max_ram_mb",
            "max_disk_gb",
        )
    }
    if all(value is None for value in values.values()):
        raise HTTPException(
            status_code=422, detail="At least one quota limit is required"
        )
    for key, value in changes.items():
        setattr(quota, key, value)
    db.commit()
    db.refresh(quota)
    return _quota_payload(db, quota)


@router.delete("/resellers/quotas/{quota_id}", status_code=204)
async def delete_stock_quota(quota_id: int, db: Session = Depends(get_db)):
    quota = db.get(StockQuota, quota_id)
    if quota is None:
        raise HTTPException(status_code=404, detail="Stock quota not found")
    db.delete(quota)
    db.commit()
    return Response(status_code=204)


class PaymentRefundRequest(RequestModel):
    reason: Optional[str] = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


def _invoice_base_options():
    return (
        joinedload(Invoice.reseller).joinedload(Reseller.user),
        joinedload(Invoice.service),
    )


def _invoice_detail_options():
    return (
        *_invoice_base_options(),
        joinedload(Invoice.payments).joinedload(Payment.invoice),
    )


@router.get("/resellers/invoices")
async def list_admin_reseller_invoices(
    reseller_id: Optional[int] = None,
    invoice_status: Optional[InvoiceStatus] = Query(default=None, alias="status"),
    purpose: Optional[InvoicePurpose] = None,
    q: Optional[str] = Query(default=None, max_length=128),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(Invoice).options(*_invoice_base_options())
    if reseller_id is not None:
        _reseller_or_404(db, reseller_id)
        stmt = stmt.where(Invoice.reseller_id == reseller_id)
    if invoice_status is not None:
        stmt = stmt.where(Invoice.status == invoice_status)
    if purpose is not None:
        stmt = stmt.where(Invoice.purpose == purpose)
    if q:
        needle = q.strip()
        if needle:
            filters = [Invoice.description.ilike(f"%{needle}%")]
            if needle.isdigit():
                filters.append(Invoice.invoice_number == int(needle))
                filters.append(Invoice.id == int(needle))
                filters.append(Invoice.service_id == int(needle))
            stmt = stmt.where(or_(*filters))
    rows = list(
        db.execute(
            stmt.order_by(Invoice.invoice_number.desc()).offset(offset).limit(limit)
        )
        .unique()
        .scalars()
    )
    return [_invoice_payload(db, row) for row in rows]


@router.get("/resellers/invoices/{invoice_id}")
async def get_admin_reseller_invoice(
    invoice_id: int, db: Session = Depends(get_db)
):
    invoice = db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(*_invoice_detail_options())
    ).unique().scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _invoice_payload(db, invoice, include_payments=True)


@router.get("/resellers/payments")
async def list_admin_reseller_payments(
    reseller_id: Optional[int] = None,
    payment_status: Optional[PaymentStatus] = Query(default=None, alias="status"),
    gateway: Optional[str] = Query(default=None, max_length=64),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Payment)
        .join(Invoice, Invoice.id == Payment.invoice_id)
        .options(joinedload(Payment.invoice))
    )
    if reseller_id is not None:
        _reseller_or_404(db, reseller_id)
        stmt = stmt.where(Invoice.reseller_id == reseller_id)
    if payment_status is not None:
        stmt = stmt.where(Payment.status == payment_status)
    if gateway:
        stmt = stmt.where(Payment.gateway == gateway.strip().lower())
    rows = list(
        db.execute(
            stmt.order_by(Payment.created_at.desc(), Payment.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .unique()
        .scalars()
    )
    return [_payment_payload(row) for row in rows]


@router.get("/resellers/payments/{payment_id}")
async def get_admin_reseller_payment(
    payment_id: int, db: Session = Depends(get_db)
):
    payment = db.execute(
        select(Payment)
        .where(Payment.id == payment_id)
        .options(joinedload(Payment.invoice))
    ).unique().scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return _payment_payload(payment)


@router.post("/resellers/payments/{payment_id}/refund")
async def refund_admin_reseller_payment(
    payment_id: int,
    body: PaymentRefundRequest,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_admin),
):
    try:
        payment = PaymentRefundService().refund(
            db,
            payment_id,
            reason=body.reason,
            admin_user_id=auth.get("user_id"),
        )
        db.commit()
    except PaymentRefundError as exc:
        db.rollback()
        status_code = status.HTTP_404_NOT_FOUND if exc.code == "not_found" else 409
        if exc.code in {"gateway_unavailable", "gateway_error", "gateway_declined"}:
            status_code = status.HTTP_502_BAD_GATEWAY
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    db.refresh(payment)
    return _payment_payload(payment)


@router.get("/resellers/billing-cycles")
async def list_admin_billing_cycles(
    reseller_id: Optional[int] = None,
    cycle_state: Optional[BillingCycleState] = Query(
        default=None, alias="state"
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(BillingCycle).join(ServiceBilling)
    if reseller_id is not None:
        _reseller_or_404(db, reseller_id)
        stmt = stmt.where(ServiceBilling.reseller_id == reseller_id)
    if cycle_state is not None:
        stmt = stmt.where(BillingCycle.state == cycle_state)
    rows = list(
        db.execute(
            stmt.order_by(BillingCycle.due_at.desc(), BillingCycle.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return [_billing_cycle_payload(row) for row in rows]


@router.post("/resellers/billing-cycles/run-due")
async def run_admin_billing_batch(
    db: Session = Depends(get_db),
):
    owner = f"admin-{uuid.uuid4().hex}"
    service = RecurringBillingService()
    acquired = service.acquire_lease(db, owner=owner)
    db.commit()
    if not acquired:
        return {"lease_acquired": False, "processed": 0}
    try:
        result = await service.process_batch(db, owner=owner)
        return {"lease_acquired": True, **result}
    finally:
        service.release_lease(db, owner=owner)
        db.commit()


@router.get("/resellers/billing-cycles/{cycle_id}")
async def get_admin_billing_cycle(
    cycle_id: int,
    db: Session = Depends(get_db),
):
    cycle = db.get(BillingCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status_code=404, detail="Billing cycle not found")
    return _billing_cycle_payload(cycle)


@router.post("/resellers/billing-cycles/{cycle_id}/retry")
async def retry_admin_billing_cycle(
    cycle_id: int,
    db: Session = Depends(get_db),
):
    cycle = db.execute(
        select(BillingCycle)
        .where(BillingCycle.id == cycle_id)
        .with_for_update()
    ).scalar_one_or_none()
    if cycle is None:
        raise HTTPException(status_code=404, detail="Billing cycle not found")
    if cycle.state in {
        BillingCycleState.PAID,
        BillingCycleState.CANCELLED,
        BillingCycleState.MANUAL_REVIEW,
    }:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry a {cycle.state.value} billing cycle",
        )
    if cycle.invoice is None:
        raise HTTPException(status_code=409, detail="Billing cycle has no invoice")
    if cycle.invoice.status == InvoiceStatus.PENDING_ACTION:
        raise HTTPException(
            status_code=409,
            detail="Payment action is still pending",
        )
    now = datetime.now(timezone.utc)
    cycle.state = BillingCycleState.GRACE
    cycle.next_retry_at = now
    cycle.claim_token = None
    cycle.claim_expires_at = None
    cycle.service_billing.status = ServiceBillingStatus.GRACE
    cycle.service_billing.next_retry_at = now
    db.commit()
    db.refresh(cycle)
    return _billing_cycle_payload(cycle)


@router.get("/resellers/notification-outbox")
async def list_admin_notification_outbox(
    reseller_id: Optional[int] = None,
    outbox_status: Optional[NotificationOutboxStatus] = Query(
        default=None, alias="status"
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(NotificationOutbox)
    if reseller_id is not None:
        _reseller_or_404(db, reseller_id)
        stmt = stmt.where(NotificationOutbox.reseller_id == reseller_id)
    if outbox_status is not None:
        stmt = stmt.where(NotificationOutbox.status == outbox_status)
    rows = list(
        db.execute(
            stmt.order_by(
                NotificationOutbox.created_at.desc(),
                NotificationOutbox.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return [_outbox_payload(row) for row in rows]


@router.post("/resellers/notification-outbox/{outbox_id}/retry")
async def retry_admin_notification(
    outbox_id: int,
    db: Session = Depends(get_db),
):
    row = db.execute(
        select(NotificationOutbox)
        .where(NotificationOutbox.id == outbox_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if row.status == NotificationOutboxStatus.SENT:
        raise HTTPException(
            status_code=409,
            detail="Sent notifications are not retried automatically",
        )
    row.status = NotificationOutboxStatus.QUEUED
    row.next_attempt_at = datetime.now(timezone.utc)
    row.error = None
    row.claim_token = None
    row.claim_expires_at = None
    db.commit()
    db.refresh(row)
    return _outbox_payload(row)


@router.get("/resellers/usdt-deposits")
async def list_admin_usdt_deposits(
    reseller_id: Optional[int] = None,
    deposit_status: Optional[UsdtDepositStatus] = Query(
        default=None, alias="status"
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(UsdtDeposit)
    if reseller_id is not None:
        _reseller_or_404(db, reseller_id)
        stmt = stmt.where(UsdtDeposit.reseller_id == reseller_id)
    if deposit_status is not None:
        stmt = stmt.where(UsdtDeposit.status == deposit_status)
    rows = list(
        db.execute(
            stmt.order_by(UsdtDeposit.created_at.desc(), UsdtDeposit.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return [_admin_usdt_payload(db, row) for row in rows]


@router.get("/resellers/usdt-deposits/{deposit_id}")
async def get_admin_usdt_deposit(
    deposit_id: int,
    db: Session = Depends(get_db),
):
    return _admin_usdt_payload(
        db,
        _usdt_deposit_or_404(db, deposit_id),
        include_events=True,
    )


@router.post("/resellers/usdt-deposits/{deposit_id}/rescan")
async def rescan_admin_usdt_deposit(
    deposit_id: int,
    body: UsdtRescanRequest,
    db: Session = Depends(get_db),
):
    deposit = _usdt_deposit_or_404(db, deposit_id)
    cursor = db.execute(
        select(UsdtChainCursor)
        .where(
            UsdtChainCursor.chain_id == deposit.chain_id,
            UsdtChainCursor.contract_address == deposit.contract_address,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if cursor is None:
        raise HTTPException(status_code=409, detail="USDT chain cursor not found")
    start = body.from_block
    if start is None:
        start = (
            int(deposit.first_block)
            if deposit.first_block is not None
            else max(
                0,
                int(cursor.next_block) - settings.usdt_reorg_recheck_blocks,
            )
        )
    cursor.next_block = min(int(cursor.next_block), int(start))
    if cursor.last_safe_block is not None and cursor.last_safe_block >= start:
        cursor.last_safe_block = None
        cursor.last_safe_block_hash = None
    db.commit()
    return {
        "deposit_id": deposit.id,
        "next_block": cursor.next_block,
        "rescan_requested": True,
    }


@router.post("/resellers/usdt-deposits/{deposit_id}/reconcile")
async def reconcile_admin_usdt_deposit(
    deposit_id: int,
    db: Session = Depends(get_db),
):
    _usdt_deposit_or_404(db, deposit_id)
    if not settings.usdt_enabled:
        raise HTTPException(status_code=503, detail="USDT deposits are not configured")
    try:
        with _admin_usdt_rpc() as rpc:
            rpc.assert_chain_id(int(settings.usdt_chain_id))
            deposit = UsdtWatcher(rpc).reconcile_deposit(db, deposit_id)
        db.commit()
        db.refresh(deposit)
        return _admin_usdt_payload(db, deposit, include_events=True)
    except EthereumRpcError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/resellers/usdt-deposits/{deposit_id}/sweep-retry")
async def retry_admin_usdt_sweep(
    deposit_id: int,
    db: Session = Depends(get_db),
):
    deposit = db.execute(
        select(UsdtDeposit)
        .where(UsdtDeposit.id == deposit_id)
        .with_for_update()
    ).scalar_one_or_none()
    if deposit is None:
        raise HTTPException(status_code=404, detail=_USDT_DEPOSIT_NOT_FOUND)
    if deposit.credited_at is None:
        raise HTTPException(status_code=409, detail="Deposit has not been credited")
    deposit.status = UsdtDepositStatus.SWEEP_PENDING
    deposit.sweep_attempts = 0
    deposit.sweep_error = None
    db.commit()
    db.refresh(deposit)
    return _admin_usdt_payload(db, deposit)


@router.post("/resellers/usdt-deposits/{deposit_id}/manual-review")
async def mark_admin_usdt_manual_review(
    deposit_id: int,
    body: UsdtManualReviewRequest,
    db: Session = Depends(get_db),
):
    deposit = db.execute(
        select(UsdtDeposit)
        .where(UsdtDeposit.id == deposit_id)
        .with_for_update()
    ).scalar_one_or_none()
    if deposit is None:
        raise HTTPException(status_code=404, detail=_USDT_DEPOSIT_NOT_FOUND)
    deposit.status = UsdtDepositStatus.MANUAL_REVIEW
    if body.reason and body.reason.strip():
        deposit.sweep_error = f"Manual review: {body.reason.strip()}"
    db.commit()
    db.refresh(deposit)
    return _admin_usdt_payload(db, deposit)


@router.get("/resellers")
async def list_resellers(
    q: Optional[str] = None,
    reseller_status: Optional[ResellerStatus] = Query(default=None, alias="status"),
    group_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    stmt = select(Reseller).join(User, User.id == Reseller.user_id)
    if reseller_status is not None:
        stmt = stmt.where(Reseller.status == reseller_status)
    if group_id is not None:
        _group_or_404(db, group_id)
        stmt = stmt.where(Reseller.group_id == group_id)
    if q and q.strip():
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.username).like(needle),
                func.lower(User.email).like(needle),
                func.lower(Reseller.api_key_prefix).like(needle),
            )
        )
    rows = list(db.execute(stmt.order_by(User.username)).scalars())
    return [_reseller_payload(db, row, include_details=True) for row in rows]


@router.post("/resellers", status_code=status.HTTP_201_CREATED)
async def create_reseller(
    body: ResellerCreate, db: Session = Depends(get_db)
):
    username_owner = db.execute(
        select(User).where(func.lower(User.username) == body.username.lower())
    ).scalar_one_or_none()
    email_owner = db.execute(
        select(User).where(func.lower(User.email) == str(body.email).lower())
    ).scalar_one_or_none()
    collision = username_owner or email_owner
    if collision is not None:
        role = "client"
        if collision.is_admin:
            role = "admin"
        elif collision.is_reseller:
            role = "reseller"
        raise HTTPException(
            status_code=409,
            detail=f"Username or email is already assigned to a {role} account",
        )
    if body.group_id is not None:
        group = _group_or_404(db, body.group_id)
        if not group.enabled:
            raise HTTPException(
                status_code=409, detail="Cannot assign a disabled reseller group"
            )
    user = User(
        username=body.username,
        email=str(body.email),
        is_admin=False,
        is_reseller=True,
    )
    try:
        user.set_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reseller = Reseller(
        user=user,
        group_id=body.group_id,
        status=body.status,
        charge_preference=body.charge_preference,
        nonpayment_policy=body.nonpayment_policy,
    )
    db.add(reseller)
    try:
        db.flush()
        plaintext_key = issue_reseller_api_key(db, reseller)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Username or email is already in use"
        ) from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(reseller)
    return {**_reseller_payload(db, reseller, include_details=True), "api_key": plaintext_key}


@router.get("/resellers/{reseller_id}")
async def get_reseller(reseller_id: int, db: Session = Depends(get_db)):
    return _reseller_payload(
        db, _reseller_or_404(db, reseller_id), include_details=True
    )


def _validate_reseller_group_change(
    db: Session, reseller: Reseller, changes: dict
) -> None:
    group_id = changes.get("group_id")
    if group_id is None or group_id == reseller.group_id:
        return
    if not _group_or_404(db, group_id).enabled:
        raise HTTPException(
            status_code=409, detail="Cannot assign a disabled reseller group"
        )


def _apply_reseller_identity_changes(
    db: Session, reseller: Reseller, changes: dict
) -> None:
    if "username" in changes:
        username = changes.pop("username")
        duplicate = db.execute(
            select(User).where(
                User.id != reseller.user_id,
                func.lower(User.username) == username.lower(),
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="Username already in use")
        reseller.user.username = username
    if "email" not in changes:
        return
    email = str(changes.pop("email"))
    duplicate = db.execute(
        select(User).where(
            User.id != reseller.user_id,
            func.lower(User.email) == email.lower(),
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="Email already in use")
    reseller.user.email = email


def _apply_billing_hold_change(reseller: Reseller, changes: dict) -> None:
    hold_change = changes.pop("billing_hold", None)
    hold_reason_present = "billing_hold_reason" in changes
    hold_reason = changes.pop("billing_hold_reason", None)
    now = datetime.now(timezone.utc)
    if hold_change is True:
        if not reseller.billing_hold:
            reseller.billing_hold_at = now
        reseller.billing_hold = True
        reseller.billing_hold_cleared_at = None
        reseller.billing_hold_reason = (hold_reason or "").strip()
        return
    if hold_change is False:
        reseller.billing_hold = False
        reseller.billing_hold_reason = None
        reseller.billing_hold_cleared_at = now
        return
    if hold_reason_present:
        if not reseller.billing_hold:
            raise HTTPException(
                status_code=409,
                detail="Cannot set a billing hold reason when no hold is active",
            )
        reseller.billing_hold_reason = (hold_reason or "").strip() or None


@router.put("/resellers/{reseller_id}")
async def update_reseller(
    reseller_id: int,
    body: ResellerUpdate,
    db: Session = Depends(get_db),
):
    reseller = _reseller_or_404(db, reseller_id)
    changes = body.model_dump(exclude_unset=True)
    _validate_reseller_group_change(db, reseller, changes)
    _apply_reseller_identity_changes(db, reseller, changes)
    _apply_billing_hold_change(reseller, changes)
    for key, value in changes.items():
        setattr(reseller, key, value)
    reseller.user.is_admin = False
    reseller.user.is_reseller = True
    db.commit()
    db.refresh(reseller)
    return _reseller_payload(db, reseller, include_details=True)


@router.delete("/resellers/{reseller_id}")
async def disable_reseller(reseller_id: int, db: Session = Depends(get_db)):
    reseller = _reseller_or_404(db, reseller_id)
    reseller.status = ResellerStatus.DISABLED
    db.commit()
    db.refresh(reseller)
    return _reseller_payload(db, reseller, include_details=True)


@router.post("/resellers/{reseller_id}/rotate-key")
async def rotate_reseller_key(
    reseller_id: int, db: Session = Depends(get_db)
):
    reseller = _reseller_or_404(db, reseller_id)
    plaintext_key = issue_reseller_api_key(db, reseller)
    db.commit()
    db.refresh(reseller)
    return {
        "reseller_id": reseller.id,
        "api_key_prefix": reseller.api_key_prefix,
        "api_key": plaintext_key,
    }


@router.get("/reseller-groups/{group_id}/prices")
async def list_group_product_prices(
    group_id: int, db: Session = Depends(get_db)
):
    _group_or_404(db, group_id)
    products = list(db.execute(select(Product).order_by(Product.name)).scalars())
    bases = {
        row.product_id: row
        for row in db.execute(select(ProductPrice)).scalars()
    }
    overrides = {
        row.product_id: row
        for row in db.execute(
            select(ResellerGroupPrice).where(
                ResellerGroupPrice.group_id == group_id
            )
        ).scalars()
    }
    return [
        _group_price_payload(
            product, bases.get(product.id), overrides.get(product.id)
        )
        for product in products
    ]


@router.put("/reseller-groups/{group_id}/prices/{product_id}")
async def upsert_group_product_price(
    group_id: int,
    product_id: int,
    body: GroupPriceUpsert,
    db: Session = Depends(get_db),
):
    _group_or_404(db, group_id)
    product = _product_or_404(db, product_id)
    base = db.execute(
        select(ProductPrice).where(ProductPrice.product_id == product_id)
    ).scalar_one_or_none()
    override = db.execute(
        select(ResellerGroupPrice).where(
            ResellerGroupPrice.group_id == group_id,
            ResellerGroupPrice.product_id == product_id,
        )
    ).scalar_one_or_none()
    if override is None:
        override = ResellerGroupPrice(group_id=group_id, product_id=product_id)
        db.add(override)
    override.setup_cents = body.setup_cents
    override.monthly_cents = body.monthly_cents
    db.commit()
    db.refresh(override)
    return _group_price_payload(product, base, override)


@router.delete(
    "/reseller-groups/{group_id}/prices/{product_id}", status_code=204
)
async def delete_group_product_price(
    group_id: int, product_id: int, db: Session = Depends(get_db)
):
    _group_or_404(db, group_id)
    _product_or_404(db, product_id)
    override = db.execute(
        select(ResellerGroupPrice).where(
            ResellerGroupPrice.group_id == group_id,
            ResellerGroupPrice.product_id == product_id,
        )
    ).scalar_one_or_none()
    if override is None:
        raise HTTPException(status_code=404, detail="Group price override not found")
    db.delete(override)
    db.commit()
    return Response(status_code=204)


@router.get("/reseller-groups/{group_id}/product-access")
async def list_group_product_access(
    group_id: int, db: Session = Depends(get_db)
):
    _group_or_404(db, group_id)
    products = list(db.execute(select(Product).order_by(Product.name)).scalars())
    rules = {
        row.product_id: row
        for row in db.execute(
            select(ResellerProductAccess).where(
                ResellerProductAccess.group_id == group_id
            )
        ).scalars()
    }
    return [
        {
            "product": _product_payload(product),
            "product_id": product.id,
            "group_rule": (
                {"id": rule.id, "allowed": rule.allowed}
                if (rule := rules.get(product.id))
                else None
            ),
            "effective_allowed": rule.allowed if rule else False,
            "effective_source": "group" if rule else "default_deny",
            "default_deny": rule is None,
        }
        for product in products
    ]


@router.put("/reseller-groups/{group_id}/product-access/{product_id}")
async def upsert_group_product_access(
    group_id: int,
    product_id: int,
    body: ProductAccessUpsert,
    db: Session = Depends(get_db),
):
    _group_or_404(db, group_id)
    _product_or_404(db, product_id)
    rule = db.execute(
        select(ResellerProductAccess).where(
            ResellerProductAccess.group_id == group_id,
            ResellerProductAccess.product_id == product_id,
        )
    ).scalar_one_or_none()
    if rule is None:
        rule = ResellerProductAccess(
            group_id=group_id, product_id=product_id
        )
        db.add(rule)
    rule.allowed = body.allowed
    db.commit()
    return await list_group_product_access(group_id, db)


@router.delete(
    "/reseller-groups/{group_id}/product-access/{product_id}",
    status_code=204,
)
async def delete_group_product_access(
    group_id: int, product_id: int, db: Session = Depends(get_db)
):
    _group_or_404(db, group_id)
    _product_or_404(db, product_id)
    rule = db.execute(
        select(ResellerProductAccess).where(
            ResellerProductAccess.group_id == group_id,
            ResellerProductAccess.product_id == product_id,
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Group access rule not found")
    db.delete(rule)
    db.commit()
    return Response(status_code=204)


@router.get("/resellers/{reseller_id}/product-access")
async def list_reseller_product_access(
    reseller_id: int, db: Session = Depends(get_db)
):
    reseller = _reseller_or_404(db, reseller_id)
    products = list(db.execute(select(Product).order_by(Product.name)).scalars())
    direct_rules = {
        row.product_id: row
        for row in db.execute(
            select(ResellerProductAccess).where(
                ResellerProductAccess.reseller_id == reseller.id
            )
        ).scalars()
    }
    group_rules = (
        {
            row.product_id: row
            for row in db.execute(
                select(ResellerProductAccess).where(
                    ResellerProductAccess.group_id == reseller.group_id
                )
            ).scalars()
        }
        if reseller.group_id is not None
        else {}
    )
    result = []
    for product in products:
        direct = direct_rules.get(product.id)
        group = group_rules.get(product.id)
        effective = direct if direct is not None else group
        effective_source = "default_deny"
        if direct is not None:
            effective_source = "direct"
        elif group is not None:
            effective_source = "group"
        result.append(
            {
                "product": _product_payload(product),
                "product_id": product.id,
                "direct_rule": (
                    {"id": direct.id, "allowed": direct.allowed} if direct else None
                ),
                "group_rule": (
                    {"id": group.id, "allowed": group.allowed} if group else None
                ),
                "effective_allowed": effective.allowed if effective else False,
                "effective_source": effective_source,
                "default_deny": effective is None,
            }
        )
    return result


@router.put("/resellers/{reseller_id}/product-access/{product_id}")
async def upsert_reseller_product_access(
    reseller_id: int,
    product_id: int,
    body: ProductAccessUpsert,
    db: Session = Depends(get_db),
):
    _reseller_or_404(db, reseller_id)
    _product_or_404(db, product_id)
    rule = db.execute(
        select(ResellerProductAccess).where(
            ResellerProductAccess.reseller_id == reseller_id,
            ResellerProductAccess.product_id == product_id,
        )
    ).scalar_one_or_none()
    if rule is None:
        rule = ResellerProductAccess(
            reseller_id=reseller_id, product_id=product_id
        )
        db.add(rule)
    rule.allowed = body.allowed
    db.commit()
    return await list_reseller_product_access(reseller_id, db)


@router.delete(
    "/resellers/{reseller_id}/product-access/{product_id}",
    status_code=204,
)
async def delete_reseller_product_access(
    reseller_id: int, product_id: int, db: Session = Depends(get_db)
):
    _reseller_or_404(db, reseller_id)
    _product_or_404(db, product_id)
    rule = db.execute(
        select(ResellerProductAccess).where(
            ResellerProductAccess.reseller_id == reseller_id,
            ResellerProductAccess.product_id == product_id,
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Direct access rule not found")
    db.delete(rule)
    db.commit()
    return Response(status_code=204)


@router.post("/resellers/{reseller_id}/credit-adjustments")
async def adjust_reseller_credit(
    reseller_id: int,
    body: CreditAdjustmentCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _reseller_or_404(db, reseller_id)
    idempotency_key = body.idempotency_key or (
        f"admin-adjustment:{uuid.uuid4()}"
    )
    try:
        entry = CreditLedgerService.adjust(
            db,
            reseller_id,
            body.amount_cents,
            description=body.reason,
            reference_type="admin_manual_adjustment",
            reference_id=str(auth["user_id"]),
            idempotency_key=idempotency_key,
            metadata={
                "reason": body.reason,
                "admin_user_id": auth["user_id"],
                "source": "reseller_admin_api",
            },
            created_by_user_id=auth["user_id"],
        )
        db.commit()
    except InsufficientCreditError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "insufficient_credit",
                "available_cents": exc.available_cents,
                "requested_cents": exc.requested_cents,
            },
        ) from exc
    except CreditLedgerError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reseller = _reseller_or_404(db, reseller_id)
    return {
        "new_balance_cents": reseller.cached_balance_cents,
        "entry": _ledger_payload(entry),
    }


@router.get("/resellers/{reseller_id}/ledger")
async def list_reseller_ledger(
    reseller_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    _reseller_or_404(db, reseller_id)
    rows = list(
        db.execute(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.reseller_id == reseller_id)
            .order_by(
                CreditLedgerEntry.created_at.desc(),
                CreditLedgerEntry.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return [_ledger_payload(row) for row in rows]
