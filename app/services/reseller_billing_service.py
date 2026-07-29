"""Reseller pricing, quota, credit, and deploy-billing orchestration."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.client_permissions import (
    DEFAULT_PERMISSIONS_BY_SERVICE_TYPE,
    PERMISSION_CATALOG,
)
from app.dao.product_catalog_dao import ProductDAO
from app.dao.reseller_dao import EffectiveProductPrice, ResellerDAO
from app.dao.user_dao import UserDAO
from app.dao.vm_config_dao import ProductVMConfigDAO
from app.models.product_catalog import Product
from app.models.reseller import (
    CreditLedgerEntry,
    Invoice,
    InvoiceStatus,
    Reseller,
    ResellerClientProductPermission,
    ResellerProvisioningRequest,
    ResellerProvisioningRequestStatus,
    ServiceBilling,
    ServiceBillingStatus,
    StockQuota,
    StockQuotaScope,
)
from app.models.service import Service, ServiceType
from app.models.user import User
from app.services.credit_ledger_service import (
    CreditLedgerService,
)
from app.services.invoice_service import InvoiceService


_USERNAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class ResellerBillingError(ValueError):
    pass


class ProductNotAvailableError(ResellerBillingError):
    pass


class QuotaNotAllocatedError(ResellerBillingError):
    pass


class QuotaExceededError(ResellerBillingError):
    pass


class IdempotencyConflictError(ResellerBillingError):
    pass


class DeployCreditRequiredError(ResellerBillingError):
    def __init__(
        self,
        *,
        required_cents: int,
        available_cents: int,
        invoice_id: int,
        pending_action: bool = False,
        client_secret: Optional[str] = None,
        payment_id: Optional[int] = None,
    ) -> None:
        self.required_cents = required_cents
        self.available_cents = available_cents
        self.shortfall_cents = max(0, required_cents - available_cents)
        self.invoice_id = invoice_id
        self.pending_action = pending_action
        self.client_secret = client_secret
        self.payment_id = payment_id
        super().__init__("Insufficient reseller credit")


class DeployPaymentFallback(Protocol):
    """Phase-four extension point for funding an open deploy invoice."""

    def fund_invoice(
        self, db: Session, reseller: Reseller, invoice: Invoice
    ) -> Any: ...


@dataclass(frozen=True)
class PreparedDeployCharge:
    product: Product
    price: EffectiveProductPrice
    invoice: Invoice
    ledger_entries: tuple[CreditLedgerEntry, ...]
    idempotency: Optional[ResellerProvisioningRequest]

    @property
    def ledger_entry(self) -> Optional[CreditLedgerEntry]:
        return self.ledger_entries[0] if self.ledger_entries else None


@dataclass(frozen=True)
class IdempotentDeployResult:
    service: Service
    invoice: Invoice


class ResellerBillingService:
    @staticmethod
    def product_permission_ceiling(product: Product) -> dict[str, bool]:
        """Maximum downstream-client permissions allowed by Rackflow."""
        if product.family is None:
            return {}
        try:
            service_type = ServiceType(product.family.service_type)
        except ValueError:
            return {}
        applicable = {
            entry["key"]
            for entry in PERMISSION_CATALOG
            if service_type.value in entry["service_types"]
        }
        ceiling = dict.fromkeys(applicable, False)
        ceiling.update(
            {
                key: bool(value)
                for key, value in DEFAULT_PERMISSIONS_BY_SERVICE_TYPE.get(
                    service_type, {}
                ).items()
                if key in applicable
            }
        )
        if product.permission_set is not None:
            ceiling.update(
                {
                    key: bool(value)
                    for key, value in (
                        product.permission_set.permissions or {}
                    ).items()
                    if key in applicable
                }
            )
        return ceiling

    @staticmethod
    def get_client_product_policy(
        db: Session, reseller_id: int, product_id: int
    ) -> Optional[ResellerClientProductPermission]:
        return db.execute(
            select(ResellerClientProductPermission).where(
                ResellerClientProductPermission.reseller_id == reseller_id,
                ResellerClientProductPermission.product_id == product_id,
            )
        ).scalar_one_or_none()

    @staticmethod
    def client_product_is_visible(
        db: Session, reseller_id: int, product_id: int
    ) -> bool:
        policy = ResellerBillingService.get_client_product_policy(
            db, reseller_id, product_id
        )
        return policy is None or bool(policy.visible)

    @staticmethod
    def effective_client_product_permissions(
        db: Session, reseller_id: int, product: Product
    ) -> dict[str, bool]:
        """Resolve reseller choices without ever exceeding product policy."""
        ceiling = ResellerBillingService.product_permission_ceiling(product)
        policy = ResellerBillingService.get_client_product_policy(
            db, reseller_id, product.id
        )
        if policy is None:
            return ceiling
        choices = policy.permissions or {}
        return {
            key: bool(ceiling_value and choices.get(key, ceiling_value))
            for key, ceiling_value in ceiling.items()
        }

    @staticmethod
    def request_hash(kind: str, payload: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            {"kind": kind, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def lookup_idempotent_result(
        db: Session,
        *,
        reseller: Reseller,
        idempotency_key: Optional[str],
        request_hash: str,
    ) -> Optional[IdempotentDeployResult]:
        if not idempotency_key:
            return None
        request = ResellerDAO.get_provisioning_request_for_update(
            db, reseller.id, idempotency_key
        )
        if request is None:
            return None
        if request.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used with a different request"
            )
        if (
            request.status == ResellerProvisioningRequestStatus.SUCCEEDED
            and request.service is not None
            and request.invoice is not None
        ):
            return IdempotentDeployResult(
                service=request.service, invoice=request.invoice
            )
        if (
            request.status
            == ResellerProvisioningRequestStatus.INSUFFICIENT_CREDIT
            and request.invoice is not None
        ):
            if request.invoice.status == InvoiceStatus.PAID:
                return None
            raise DeployCreditRequiredError(
                required_cents=request.invoice.amount_cents,
                available_cents=reseller.cached_balance_cents,
                invoice_id=request.invoice.id,
            )
        raise IdempotencyConflictError(
            f"Idempotent request is {request.status.value}"
        )

    @staticmethod
    def resolve_sellable_product(
        db: Session, reseller: Reseller, product_code: str
    ) -> tuple[Product, EffectiveProductPrice]:
        product = ProductDAO.get_by_code(db, product_code)
        if (
            product is None
            or not product.enabled
            or product.family is None
            or not product.family.enabled
        ):
            raise ProductNotAvailableError("Product not found")
        if not ResellerDAO.product_is_allowed(db, reseller.id, product.id):
            raise ProductNotAvailableError("Product not found")
        price = ResellerDAO.get_effective_price(
            db, reseller.id, product.id
        )
        if price is None:
            raise ProductNotAvailableError("Product has no effective price")
        return product, price

    @staticmethod
    def list_sellable_products(
        db: Session, reseller: Reseller
    ) -> list[tuple[Product, EffectiveProductPrice]]:
        result: list[tuple[Product, EffectiveProductPrice]] = []
        for product in ProductDAO.get_all(db):
            try:
                result.append(
                    ResellerBillingService.resolve_sellable_product(
                        db, reseller, product.code
                    )
                )
            except ProductNotAvailableError:
                continue
        return result

    @staticmethod
    def _refresh_existing_client(
        db: Session,
        existing: User,
        external_username: Optional[str],
        external_email: Optional[str],
    ) -> User:
        changes = {
            "external_username": external_username,
            "external_email": external_email,
        }
        changed = False
        for field, value in changes.items():
            if value and getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True
        if changed:
            UserDAO.update(db, existing)
        return existing

    @staticmethod
    def _unique_client_username(
        db: Session,
        reseller_id: int,
        external_user_id: str,
        external_username: Optional[str],
        digest: str,
    ) -> str:
        hint = external_username or f"client-{external_user_id}"
        safe_hint = (
            _USERNAME_SAFE_RE.sub("-", hint).strip(".-_") or "client"
        )
        base = f"rf-r{reseller_id}-{safe_hint}"[:220]
        root = base
        if UserDAO.get_by_username(db, root):
            root = f"{base[:207]}-{digest}"
        candidate = root
        suffix = 0
        while UserDAO.get_by_username(db, candidate):
            suffix += 1
            candidate = f"{root[:240]}-{suffix}"
        return candidate

    @staticmethod
    def _unique_client_email(
        db: Session,
        reseller_id: int,
        external_email: Optional[str],
        digest: str,
    ) -> str:
        requested = (external_email or "").strip().lower()
        if requested and not UserDAO.get_by_email(db, requested):
            return requested
        root = f"r{reseller_id}-{digest}"
        candidate = f"{root}@clients.rackflow.local"
        suffix = 0
        while UserDAO.get_by_email(db, candidate):
            suffix += 1
            candidate = f"{root}-{suffix}@clients.rackflow.local"
        return candidate

    @staticmethod
    def ensure_client_user(
        db: Session,
        *,
        reseller: Reseller,
        external_user_id: str,
        external_username: Optional[str] = None,
        external_email: Optional[str] = None,
    ) -> User:
        external_user_id = str(external_user_id or "").strip()
        if not external_user_id:
            raise ResellerBillingError("external_user_id is required")
        existing = UserDAO.get_by_reseller_identity(
            db, reseller.id, external_user_id
        )
        if existing is not None:
            if existing.id == reseller.user_id or existing.is_reseller:
                raise ResellerBillingError(
                    "Reseller account cannot own deployed services"
                )
            return ResellerBillingService._refresh_existing_client(
                db, existing, external_username, external_email
            )

        digest = hashlib.sha256(
            f"{reseller.id}:{external_user_id}".encode("utf-8")
        ).hexdigest()[:12]
        username = ResellerBillingService._unique_client_username(
            db,
            reseller.id,
            external_user_id,
            external_username,
            digest,
        )
        email = ResellerBillingService._unique_client_email(
            db, reseller.id, external_email, digest
        )

        user = UserDAO.create(
            db,
            username=username,
            email=email,
            password=None,
            is_admin=False,
            is_reseller=False,
            reseller_id=reseller.id,
            external_user_id=external_user_id,
            external_username=external_username,
            external_email=external_email,
        )
        if user.id == reseller.user_id or user.is_reseller:
            raise ResellerBillingError(
                "Reseller account cannot own deployed services"
            )
        return user

    @staticmethod
    def _effective_specs(db: Session, product: Product) -> dict[str, Any]:
        specs = ProductVMConfigDAO.resolve_effective_config(db, product)
        if specs:
            return dict(specs)
        family = product.family
        return {
            **((family.defaults if family else None) or {}),
            **(product.overrides or {}),
        }

    @staticmethod
    def _resource_values(specs: Mapping[str, Any]) -> tuple[int, int, int]:
        def _int_value(*keys: str) -> int:
            for key in keys:
                value = specs.get(key)
                if value not in (None, ""):
                    try:
                        return max(0, int(value))
                    except (TypeError, ValueError):
                        return 0
            return 0

        cpu = _int_value("cpu_cores", "cpu_count", "cores")
        ram = _int_value("ram_mb", "memory_mb")
        if ram == 0:
            ram = _int_value("ram_gb", "memory_gb") * 1024
        disk = _int_value("disk_gb", "storage_gb", "disk_size_gb")
        return cpu, ram, disk

    @staticmethod
    def _billing_matches_scope(
        billing: ServiceBilling,
        scope_type: StockQuotaScope,
        scope_id: int,
    ) -> bool:
        if scope_type == StockQuotaScope.PRODUCT:
            return billing.product_id == scope_id
        service = billing.service
        if service is None:
            return False
        if scope_type == StockQuotaScope.SERVER_GROUP:
            config = service.config or {}
            try:
                return int(config.get("server_group_id")) == scope_id
            except (TypeError, ValueError):
                return False
        if scope_type == StockQuotaScope.PROXMOX_CLUSTER:
            return bool(
                service.vm
                and service.vm.proxmox_cluster_id == scope_id
            )
        return False

    @staticmethod
    def _quota_scopes(
        product_id: int,
        server_group_id: Optional[int],
        proxmox_cluster_id: Optional[int],
    ) -> list[tuple[StockQuotaScope, int]]:
        scopes = [(StockQuotaScope.PRODUCT, product_id)]
        if server_group_id is not None:
            scopes.append(
                (StockQuotaScope.SERVER_GROUP, int(server_group_id))
            )
        if proxmox_cluster_id is not None:
            scopes.append(
                (
                    StockQuotaScope.PROXMOX_CLUSTER,
                    int(proxmox_cluster_id),
                )
            )
        return scopes

    @staticmethod
    def _locked_quotas(
        db: Session,
        reseller_id: int,
        scopes: list[tuple[StockQuotaScope, int]],
    ) -> list[StockQuota]:
        quotas = [
            quota
            for scope_type, scope_id in scopes
            if (
                quota := ResellerDAO.get_effective_stock_quota_for_update(
                    db, reseller_id, scope_type, scope_id
                )
            )
            is not None
        ]
        if not quotas:
            raise QuotaNotAllocatedError(
                "No product or matching resource quota is allocated"
            )
        return quotas

    @staticmethod
    def _used_resources(
        billings: list[ServiceBilling],
    ) -> tuple[int, int, int]:
        totals = [0, 0, 0]
        for billing in billings:
            service = billing.service
            specs = (
                (service.product_snapshot or {}).get("effective_specs", {})
                if service is not None
                else {}
            )
            values = ResellerBillingService._resource_values(specs)
            totals = [current + value for current, value in zip(totals, values)]
        return totals[0], totals[1], totals[2]

    @staticmethod
    def _enforce_quota(
        quota: StockQuota,
        matching: list[ServiceBilling],
        requested: tuple[int, int, int],
    ) -> None:
        label = f"{quota.scope_type.value}:{quota.scope_id}"
        if (
            quota.max_services is not None
            and len(matching) + 1 > quota.max_services
        ):
            raise QuotaExceededError(
                f"Service-count quota exceeded for {label}"
            )
        used = ResellerBillingService._used_resources(matching)
        limits = (
            ("CPU", quota.max_cpu_cores),
            ("RAM", quota.max_ram_mb),
            ("disk", quota.max_disk_gb),
        )
        for resource, limit, consumed, addition in zip(
            (row[0] for row in limits),
            (row[1] for row in limits),
            used,
            requested,
        ):
            if limit is not None and consumed + addition > limit:
                raise QuotaExceededError(
                    f"{resource} quota exceeded for {label}"
                )

    @staticmethod
    def enforce_quotas(
        db: Session,
        *,
        reseller: Reseller,
        product: Product,
        server_group_id: Optional[int] = None,
        proxmox_cluster_id: Optional[int] = None,
    ) -> None:
        # Consistent lock order: reseller balance first, then matching quotas.
        locked_reseller = ResellerDAO.get_for_update(db, reseller.id)
        if locked_reseller is None:
            raise ResellerBillingError("Reseller not found")

        scopes = ResellerBillingService._quota_scopes(
            product.id, server_group_id, proxmox_cluster_id
        )
        quotas = ResellerBillingService._locked_quotas(
            db, reseller.id, scopes
        )
        requested = ResellerBillingService._resource_values(
            ResellerBillingService._effective_specs(db, product)
        )
        billings = ResellerDAO.list_service_billings(
            db,
            reseller.id,
            statuses=[
                ServiceBillingStatus.ACTIVE,
                ServiceBillingStatus.PAST_DUE,
                ServiceBillingStatus.GRACE,
                ServiceBillingStatus.SUSPENDED_NONPAYMENT,
            ],
        )
        for quota in quotas:
            matching = [
                row
                for row in billings
                if ResellerBillingService._billing_matches_scope(
                    row, quota.scope_type, quota.scope_id
                )
            ]
            ResellerBillingService._enforce_quota(
                quota, matching, requested
            )

    @staticmethod
    def _invoice_debits(
        db: Session, invoice_id: int
    ) -> tuple[CreditLedgerEntry, ...]:
        return tuple(
            db.execute(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.invoice_id == invoice_id,
                    CreditLedgerEntry.amount_cents < 0,
                )
            ).scalars()
        )

    @staticmethod
    def _prepare_deploy_idempotency(
        db: Session,
        *,
        reseller: Reseller,
        product: Product,
        price: EffectiveProductPrice,
        idempotency_key: Optional[str],
        request_hash: str,
    ) -> tuple[
        Optional[ResellerProvisioningRequest],
        Optional[PreparedDeployCharge],
    ]:
        if not idempotency_key:
            return None, None
        request = ResellerDAO.get_provisioning_request_for_update(
            db, reseller.id, idempotency_key
        )
        if request is None:
            request = ResellerProvisioningRequest(
                reseller_id=reseller.id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            db.add(request)
            db.flush()
            return request, None
        if request.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used with a different request"
            )
        resumable = (
            request.status
            == ResellerProvisioningRequestStatus.INSUFFICIENT_CREDIT
            and request.invoice is not None
            and request.invoice.status == InvoiceStatus.PAID
        )
        if not resumable:
            raise IdempotencyConflictError(
                f"Idempotent request is {request.status.value}"
            )
        request.status = ResellerProvisioningRequestStatus.PROCESSING
        return request, PreparedDeployCharge(
            product=product,
            price=price,
            invoice=request.invoice,
            ledger_entries=ResellerBillingService._invoice_debits(
                db, request.invoice.id
            ),
            idempotency=request,
        )

    @staticmethod
    def prepare_deploy_charge(
        db: Session,
        *,
        reseller: Reseller,
        product: Product,
        price: EffectiveProductPrice,
        idempotency_key: Optional[str],
        request_hash: str,
        payment_fallback: Optional[DeployPaymentFallback] = None,
    ) -> PreparedDeployCharge:
        total_cents = price.setup_cents + price.monthly_cents
        if type(total_cents) is not int or total_cents <= 0:
            raise ResellerBillingError(
                "Deploy charge must be a positive integer number of cents"
            )

        idempotency, resumed = (
            ResellerBillingService._prepare_deploy_idempotency(
                db,
                reseller=reseller,
                product=product,
                price=price,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        )
        if resumed is not None:
            return resumed

        invoice = InvoiceService.create_deploy(
            db,
            reseller_id=reseller.id,
            amount_cents=total_cents,
            description=(
                f"Deploy {product.code}: setup {price.setup_cents} + "
                f"first month {price.monthly_cents}"
            ),
            currency=price.currency,
        )
        if idempotency is not None:
            idempotency.invoice_id = invoice.id

        available_before = max(0, int(reseller.cached_balance_cents))
        if payment_fallback is None:
            from app.services.payments.orchestrator import PaymentOrchestrator

            payment_fallback = PaymentOrchestrator()
        funding = payment_fallback.fund_invoice(db, reseller, invoice)
        if not funding:
            if idempotency is not None:
                idempotency.status = (
                    ResellerProvisioningRequestStatus.INSUFFICIENT_CREDIT
                )
            pending_action = bool(
                getattr(funding, "pending_action", False)
            )
            db.commit()
            raise DeployCreditRequiredError(
                required_cents=total_cents,
                available_cents=available_before,
                invoice_id=invoice.id,
                pending_action=pending_action,
                client_secret=getattr(funding, "client_secret", None),
                payment_id=getattr(funding, "payment_id", None),
            )

        InvoiceService.mark_paid_if_fully_allocated(db, invoice.id)
        ledger_entries = ResellerBillingService._invoice_debits(
            db, invoice.id
        )
        db.commit()
        db.refresh(invoice)
        return PreparedDeployCharge(
            product=product,
            price=price,
            invoice=invoice,
            ledger_entries=ledger_entries,
            idempotency=idempotency,
        )

    @staticmethod
    def _one_month_after(value: datetime) -> datetime:
        year = value.year + (1 if value.month == 12 else 0)
        month = 1 if value.month == 12 else value.month + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return value.replace(year=year, month=month, day=day)

    @staticmethod
    def finalize_deploy(
        db: Session,
        *,
        reseller: Reseller,
        service: Service,
        charge: PreparedDeployCharge,
    ) -> ServiceBilling:
        now = datetime.now(timezone.utc)
        service_billing = ServiceBilling(
            service_id=service.id,
            reseller_id=reseller.id,
            product_id=charge.product.id,
            setup_price_cents=charge.price.setup_cents,
            monthly_price_cents=charge.price.monthly_cents,
            currency=charge.price.currency,
            next_charge_at=ResellerBillingService._one_month_after(now),
            status=ServiceBillingStatus.ACTIVE,
            last_charged_at=now,
        )
        db.add(service_billing)
        charge.invoice.service_id = service.id
        for ledger_entry in charge.ledger_entries:
            ledger_entry.service_id = service.id
        if charge.idempotency is not None:
            charge.idempotency.service_id = service.id
            charge.idempotency.status = (
                ResellerProvisioningRequestStatus.SUCCEEDED
            )
        db.commit()
        db.refresh(service_billing)
        return service_billing

    @staticmethod
    def compensate_failed_deploy(
        db: Session,
        *,
        reseller: Reseller,
        charge: PreparedDeployCharge,
        error: Exception,
    ) -> None:
        for ledger_entry in charge.ledger_entries:
            CreditLedgerService.reverse(
                db,
                reseller.id,
                ledger_entry.id,
                description=(
                    f"Failed deploy refund for invoice {charge.invoice.id}"
                ),
                idempotency_key=(
                    f"reseller-deploy-refund:{charge.invoice.id}:"
                    f"{ledger_entry.id}"
                ),
                metadata={"reason": str(error)[:500]},
                created_by_user_id=reseller.user_id,
            )
        charge.invoice.status = InvoiceStatus.VOID
        if charge.idempotency is not None:
            charge.idempotency.status = (
                ResellerProvisioningRequestStatus.FAILED
            )
            charge.idempotency.error_message = str(error)[:2000]
        db.commit()
