"""Database queries for reseller pricing, access, quotas, and billing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.models.reseller import (
    CreditLedgerEntry,
    Invoice,
    InvoiceStatus,
    Payment,
    ProductPrice,
    Reseller,
    ResellerGroupPrice,
    ResellerPaymentMethod,
    ResellerProductAccess,
    ResellerProvisioningRequest,
    ServiceBilling,
    ServiceBillingStatus,
    StockQuota,
    StockQuotaScope,
)
from app.models.server_group import server_group_association
from app.models.service_bare_metal import ServiceBareMetal
from app.models.service_vm import ServiceVm


@dataclass(frozen=True)
class EffectiveProductPrice:
    """Resolved price after applying nullable group-level overrides."""

    product_id: int
    setup_cents: int
    monthly_cents: int
    currency: str
    setup_source: str
    monthly_source: str


class ResellerDAO:
    @staticmethod
    def get_by_id(db: Session, reseller_id: int) -> Optional[Reseller]:
        return db.execute(
            select(Reseller).where(Reseller.id == reseller_id)
        ).scalar_one_or_none()

    @staticmethod
    def get_by_user_id(db: Session, user_id: int) -> Optional[Reseller]:
        return db.execute(
            select(Reseller).where(Reseller.user_id == user_id)
        ).scalar_one_or_none()

    @staticmethod
    def get_by_api_key_hash(db: Session, api_key_hash: str) -> Optional[Reseller]:
        return db.execute(
            select(Reseller).where(Reseller.api_key_hash == api_key_hash)
        ).scalar_one_or_none()

    @staticmethod
    def get_for_update(db: Session, reseller_id: int) -> Optional[Reseller]:
        """Lock one reseller balance row for the caller's transaction."""
        return db.execute(
            select(Reseller)
            .where(Reseller.id == reseller_id)
            .with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def get_effective_price(
        db: Session, reseller_id: int, product_id: int
    ) -> Optional[EffectiveProductPrice]:
        reseller = ResellerDAO.get_by_id(db, reseller_id)
        if reseller is None:
            return None

        base = db.execute(
            select(ProductPrice).where(ProductPrice.product_id == product_id)
        ).scalar_one_or_none()
        if base is None:
            return None

        override = None
        if reseller.group_id is not None:
            override = db.execute(
                select(ResellerGroupPrice).where(
                    ResellerGroupPrice.group_id == reseller.group_id,
                    ResellerGroupPrice.product_id == product_id,
                )
            ).scalar_one_or_none()

        setup_overridden = override is not None and override.setup_cents is not None
        monthly_overridden = (
            override is not None and override.monthly_cents is not None
        )
        return EffectiveProductPrice(
            product_id=product_id,
            setup_cents=(
                override.setup_cents if setup_overridden else base.setup_cents
            ),
            monthly_cents=(
                override.monthly_cents if monthly_overridden else base.monthly_cents
            ),
            currency=base.currency,
            setup_source="group" if setup_overridden else "base",
            monthly_source="group" if monthly_overridden else "base",
        )

    @staticmethod
    def product_is_allowed(db: Session, reseller_id: int, product_id: int) -> bool:
        """Resolve reseller override, then group default, then deny."""
        reseller = ResellerDAO.get_by_id(db, reseller_id)
        if reseller is None:
            return False

        reseller_rule = db.execute(
            select(ResellerProductAccess).where(
                ResellerProductAccess.reseller_id == reseller_id,
                ResellerProductAccess.product_id == product_id,
            )
        ).scalar_one_or_none()
        if reseller_rule is not None:
            return bool(reseller_rule.allowed)

        if reseller.group_id is not None:
            group_rule = db.execute(
                select(ResellerProductAccess).where(
                    ResellerProductAccess.group_id == reseller.group_id,
                    ResellerProductAccess.product_id == product_id,
                )
            ).scalar_one_or_none()
            if group_rule is not None:
                return bool(group_rule.allowed)

        return False

    @staticmethod
    def get_effective_stock_quota(
        db: Session,
        reseller_id: int,
        scope_type: StockQuotaScope,
        scope_id: int,
        *,
        for_update: bool = False,
    ) -> Optional[StockQuota]:
        """Resolve a reseller quota before its group's default quota."""
        reseller = ResellerDAO.get_by_id(db, reseller_id)
        if reseller is None:
            return None

        direct_stmt = select(StockQuota).where(
            StockQuota.reseller_id == reseller_id,
            StockQuota.scope_type == scope_type,
            StockQuota.scope_id == scope_id,
            StockQuota.enabled.is_(True),
        )
        if for_update:
            direct_stmt = direct_stmt.with_for_update()
        direct = db.execute(direct_stmt).scalar_one_or_none()
        if direct is not None:
            return direct

        if reseller.group_id is None:
            return None
        group_stmt = select(StockQuota).where(
            StockQuota.group_id == reseller.group_id,
            StockQuota.scope_type == scope_type,
            StockQuota.scope_id == scope_id,
            StockQuota.enabled.is_(True),
        )
        if for_update:
            group_stmt = group_stmt.with_for_update()
        return db.execute(group_stmt).scalar_one_or_none()

    @staticmethod
    def get_effective_stock_quota_for_update(
        db: Session,
        reseller_id: int,
        scope_type: StockQuotaScope,
        scope_id: int,
    ) -> Optional[StockQuota]:
        """Resolve and lock the quota row for race-safe enforcement."""
        return ResellerDAO.get_effective_stock_quota(
            db,
            reseller_id,
            scope_type,
            scope_id,
            for_update=True,
        )

    @staticmethod
    def list_effective_stock_quotas(
        db: Session, reseller_id: int
    ) -> list[StockQuota]:
        """Return enabled quotas with reseller rows shadowing group rows."""
        reseller = ResellerDAO.get_by_id(db, reseller_id)
        if reseller is None:
            return []

        resolved: dict[tuple[StockQuotaScope, int], StockQuota] = {}
        if reseller.group_id is not None:
            group_rows = db.execute(
                select(StockQuota).where(
                    StockQuota.group_id == reseller.group_id,
                    StockQuota.enabled.is_(True),
                )
            ).scalars()
            resolved.update(
                {(row.scope_type, row.scope_id): row for row in group_rows}
            )

        reseller_rows = db.execute(
            select(StockQuota).where(
                StockQuota.reseller_id == reseller_id,
                StockQuota.enabled.is_(True),
            )
        ).scalars()
        resolved.update(
            {(row.scope_type, row.scope_id): row for row in reseller_rows}
        )
        return sorted(
            resolved.values(), key=lambda row: (row.scope_type.value, row.scope_id)
        )

    @staticmethod
    def _scope_usage_statement(
        reseller_id: int,
        scope_type: StockQuotaScope,
        scope_id: int,
    ):
        stmt = (
            select(ServiceBilling)
            .where(
                ServiceBilling.reseller_id == reseller_id,
                ServiceBilling.status != ServiceBillingStatus.CANCELLED,
            )
            .order_by(ServiceBilling.service_id)
        )
        if scope_type == StockQuotaScope.PRODUCT:
            return stmt.where(ServiceBilling.product_id == scope_id)
        if scope_type == StockQuotaScope.PROXMOX_CLUSTER:
            return stmt.join(
                ServiceVm, ServiceVm.service_id == ServiceBilling.service_id
            ).where(ServiceVm.proxmox_cluster_id == scope_id)
        if scope_type == StockQuotaScope.SERVER_GROUP:
            return (
                stmt.join(
                    ServiceBareMetal,
                    ServiceBareMetal.service_id == ServiceBilling.service_id,
                )
                .join(
                    server_group_association,
                    server_group_association.c.server_id
                    == ServiceBareMetal.server_id,
                )
                .where(
                    server_group_association.c.server_group_id == scope_id
                )
            )
        raise ValueError(f"Unsupported stock quota scope: {scope_type!r}")

    @staticmethod
    def list_scope_service_billings(
        db: Session,
        reseller_id: int,
        scope_type: StockQuotaScope,
        scope_id: int,
    ) -> list[ServiceBilling]:
        """Rows used by callers to calculate count and resource consumption."""
        return list(
            db.execute(
                ResellerDAO._scope_usage_statement(
                    reseller_id, scope_type, scope_id
                )
            )
            .unique()
            .scalars()
        )

    @staticmethod
    def count_scope_usage(
        db: Session,
        reseller_id: int,
        scope_type: StockQuotaScope,
        scope_id: int,
    ) -> int:
        scoped = ResellerDAO._scope_usage_statement(
            reseller_id, scope_type, scope_id
        ).order_by(None)
        count_stmt = select(
            func.count(distinct(scoped.subquery().c.service_id))
        )
        return int(db.execute(count_stmt).scalar_one())

    @staticmethod
    def get_invoice_by_number(
        db: Session, invoice_number: int
    ) -> Optional[Invoice]:
        return db.execute(
            select(Invoice).where(Invoice.invoice_number == invoice_number)
        ).scalar_one_or_none()

    @staticmethod
    def list_invoices(
        db: Session,
        reseller_id: int,
        *,
        status: Optional[InvoiceStatus] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Invoice]:
        stmt = select(Invoice).where(Invoice.reseller_id == reseller_id)
        if status is not None:
            stmt = stmt.where(Invoice.status == status)
        return list(
            db.execute(
                stmt.order_by(Invoice.invoice_number.desc())
                .offset(offset)
                .limit(limit)
            ).scalars()
        )

    @staticmethod
    def get_payment_by_external_ref(
        db: Session, gateway: str, external_ref: str
    ) -> Optional[Payment]:
        return db.execute(
            select(Payment).where(
                Payment.gateway == gateway,
                Payment.external_ref == external_ref,
            )
        ).scalar_one_or_none()

    @staticmethod
    def list_invoice_payments(
        db: Session, invoice_id: int
    ) -> list[Payment]:
        return list(
            db.execute(
                select(Payment)
                .where(Payment.invoice_id == invoice_id)
                .order_by(Payment.created_at.desc(), Payment.id.desc())
            ).scalars()
        )

    @staticmethod
    def list_payment_methods(
        db: Session,
        reseller_id: int,
        *,
        enabled_only: bool = True,
    ) -> list[ResellerPaymentMethod]:
        stmt = select(ResellerPaymentMethod).where(
            ResellerPaymentMethod.reseller_id == reseller_id
        )
        if enabled_only:
            stmt = stmt.where(ResellerPaymentMethod.enabled.is_(True))
        return list(
            db.execute(
                stmt.order_by(
                    ResellerPaymentMethod.tier,
                    ResellerPaymentMethod.id,
                )
            ).scalars()
        )

    @staticmethod
    def list_service_billings(
        db: Session,
        reseller_id: int,
        *,
        statuses: Optional[Sequence[ServiceBillingStatus]] = None,
    ) -> list[ServiceBilling]:
        stmt = select(ServiceBilling).where(
            ServiceBilling.reseller_id == reseller_id
        )
        if statuses:
            stmt = stmt.where(ServiceBilling.status.in_(statuses))
        return list(
            db.execute(stmt.order_by(ServiceBilling.service_id)).scalars()
        )

    @staticmethod
    def get_service_billing(
        db: Session, reseller_id: int, service_id: int
    ) -> Optional[ServiceBilling]:
        return db.execute(
            select(ServiceBilling).where(
                ServiceBilling.reseller_id == reseller_id,
                ServiceBilling.service_id == service_id,
            )
        ).scalar_one_or_none()

    @staticmethod
    def get_ledger_entry_by_idempotency_key(
        db: Session, idempotency_key: str
    ) -> Optional[CreditLedgerEntry]:
        return db.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()

    @staticmethod
    def get_provisioning_request_for_update(
        db: Session, reseller_id: int, idempotency_key: str
    ) -> Optional[ResellerProvisioningRequest]:
        return db.execute(
            select(ResellerProvisioningRequest)
            .where(
                ResellerProvisioningRequest.reseller_id == reseller_id,
                ResellerProvisioningRequest.idempotency_key
                == idempotency_key,
            )
            .with_for_update()
        ).scalar_one_or_none()
