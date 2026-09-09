"""Persistence models for reseller pricing, billing, and credit accounting."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import backref, relationship
from sqlalchemy.sql import func

from app.core.database import Base


_CASCADE_DELETE_ORPHAN = "all, delete-orphan"
_FK_GROUPS = "reseller_groups.id"
_FK_INVOICES = "invoices.id"
_FK_PRODUCTS = "products.id"
_FK_RESELLERS = "resellers.id"
_FK_SERVICES = "services.id"
_FK_USERS = "users.id"
_ON_DELETE_SET_NULL = "SET NULL"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _string_enum(enum_cls: type[enum.Enum], *, length: int = 32) -> SQLEnum:
    """Persist enums as VARCHAR with an explicit width.

    SQLAlchemy's default width for ``native_enum=False`` tracks the longest
    value at table-create time and is easy to undersize when vocabulary grows.
    """
    return SQLEnum(
        enum_cls,
        native_enum=False,
        values_callable=_enum_values,
        length=length,
    )


class ResellerChargePreference(str, enum.Enum):
    CREDIT_FIRST = "credit_first"
    PAYMENT_FIRST = "payment_first"


class ResellerStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DISABLED = "disabled"


class ResellerNonpaymentPolicy(str, enum.Enum):
    BLOCK_NEW = "block_new"
    SUSPEND_ALL = "suspend_all"


class StockQuotaScope(str, enum.Enum):
    PRODUCT = "product"
    SERVER_GROUP = "server_group"
    PROXMOX_CLUSTER = "proxmox_cluster"


class InvoicePurpose(str, enum.Enum):
    CREDIT_TOPUP = "credit_topup"
    DEPLOY_CHARGE = "deploy_charge"
    CYCLE_CHARGE = "cycle_charge"
    ADJUSTMENT = "adjustment"
    ORDER_CHARGE = "order_charge"
    ADDON = "addon"
    UPGRADE = "upgrade"
    CREDIT_NOTE = "credit_note"
    RENEWAL = "renewal"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    FAILED = "failed"
    PENDING_ACTION = "pending_action"
    OVERDUE = "overdue"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class CreditLedgerEntryType(str, enum.Enum):
    TOPUP = "topup"
    CHARGE = "charge"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    REVERSAL = "reversal"
    CREDIT = "topup"
    DEBIT = "charge"


class ServiceBillingStatus(str, enum.Enum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    GRACE = "grace"
    SUSPENDED_NONPAYMENT = "suspended_nonpayment"
    CANCELLED = "cancelled"
    MANUAL_REVIEW = "manual_review"
    ERROR = "error"


class BillingCycleState(str, enum.Enum):
    DUE = "due"
    PROCESSING = "processing"
    PAST_DUE = "past_due"
    GRACE = "grace"
    PENDING_ACTION = "pending_action"
    PAID = "paid"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    MANUAL_REVIEW = "manual_review"
    ERROR = "error"


class NotificationOutboxStatus(str, enum.Enum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


class ResellerProvisioningRequestStatus(str, enum.Enum):
    PROCESSING = "processing"
    INSUFFICIENT_CREDIT = "insufficient_credit"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ResellerGroup(Base):
    __tablename__ = "reseller_groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True, index=True)
    code = Column(String(64), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    resellers = relationship("Reseller", back_populates="group")
    prices = relationship(
        "ResellerGroupPrice",
        back_populates="group",
        cascade=_CASCADE_DELETE_ORPHAN,
    )
    product_access = relationship(
        "ResellerProductAccess",
        back_populates="group",
        cascade=_CASCADE_DELETE_ORPHAN,
    )
    stock_quotas = relationship(
        "StockQuota",
        back_populates="group",
        cascade=_CASCADE_DELETE_ORPHAN,
    )


class Reseller(Base):
    __tablename__ = "resellers"
    __table_args__ = (
        Index("ix_resellers_status_group", "status", "group_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    group_id = Column(
        Integer,
        ForeignKey(_FK_GROUPS, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    api_key_hash = Column(String(64), nullable=True, unique=True)
    api_key_prefix = Column(String(16), nullable=True, index=True)
    stripe_customer_ref = Column(String(255), nullable=True, unique=True, index=True)
    cached_balance_cents = Column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    charge_preference = Column(
        _string_enum(ResellerChargePreference),
        nullable=False,
        default=ResellerChargePreference.CREDIT_FIRST,
        server_default=ResellerChargePreference.CREDIT_FIRST.value,
    )
    status = Column(
        _string_enum(ResellerStatus),
        nullable=False,
        default=ResellerStatus.ACTIVE,
        server_default=ResellerStatus.ACTIVE.value,
        index=True,
    )
    nonpayment_policy = Column(
        _string_enum(ResellerNonpaymentPolicy),
        nullable=False,
        default=ResellerNonpaymentPolicy.BLOCK_NEW,
        server_default=ResellerNonpaymentPolicy.BLOCK_NEW.value,
    )
    billing_hold = Column(Boolean, nullable=False, default=False, server_default="0")
    billing_hold_at = Column(DateTime(timezone=True), nullable=True)
    billing_hold_cleared_at = Column(DateTime(timezone=True), nullable=True)
    billing_hold_reason = Column(String(512), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    last_used_ip = Column(String(45), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship(
        "User", back_populates="reseller_account", foreign_keys=[user_id]
    )
    clients = relationship(
        "User", back_populates="owning_reseller", foreign_keys="User.reseller_id"
    )
    group = relationship("ResellerGroup", back_populates="resellers")
    product_access = relationship(
        "ResellerProductAccess",
        back_populates="reseller",
        cascade=_CASCADE_DELETE_ORPHAN,
    )
    client_product_permissions = relationship(
        "ResellerClientProductPermission",
        back_populates="reseller",
        cascade=_CASCADE_DELETE_ORPHAN,
    )
    stock_quotas = relationship(
        "StockQuota",
        back_populates="reseller",
        cascade=_CASCADE_DELETE_ORPHAN,
    )
    invoices = relationship("Invoice", back_populates="reseller")
    payment_methods = relationship(
        "ResellerPaymentMethod",
        back_populates="reseller",
        cascade=_CASCADE_DELETE_ORPHAN,
    )
    ledger_entries = relationship(
        "CreditLedgerEntry",
        back_populates="reseller",
        order_by="CreditLedgerEntry.id",
    )
    service_billings = relationship("ServiceBilling", back_populates="reseller")
    provisioning_requests = relationship(
        "ResellerProvisioningRequest", back_populates="reseller"
    )


class ProductPrice(Base):
    __tablename__ = "product_prices"
    __table_args__ = (
        CheckConstraint("setup_cents >= 0", name="ck_product_prices_setup_nonnegative"),
        CheckConstraint("monthly_cents >= 0", name="ck_product_prices_monthly_nonnegative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(
        Integer,
        ForeignKey(_FK_PRODUCTS, ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    setup_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    monthly_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    product = relationship(
        "Product", backref=backref("reseller_base_price", uselist=False)
    )


class ResellerGroupPrice(Base):
    __tablename__ = "reseller_group_prices"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "product_id", name="uq_reseller_group_prices_group_product"
        ),
        CheckConstraint(
            "setup_cents IS NULL OR setup_cents >= 0",
            name="ck_reseller_group_prices_setup_nonnegative",
        ),
        CheckConstraint(
            "monthly_cents IS NULL OR monthly_cents >= 0",
            name="ck_reseller_group_prices_monthly_nonnegative",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(
        Integer,
        ForeignKey(_FK_GROUPS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id = Column(
        Integer,
        ForeignKey(_FK_PRODUCTS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    setup_cents = Column(BigInteger, nullable=True)
    monthly_cents = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    group = relationship("ResellerGroup", back_populates="prices")
    product = relationship("Product")


class ResellerProductAccess(Base):
    __tablename__ = "reseller_product_access"
    __table_args__ = (
        CheckConstraint(
            "(reseller_id IS NOT NULL AND group_id IS NULL) OR "
            "(reseller_id IS NULL AND group_id IS NOT NULL)",
            name="ck_reseller_product_access_one_owner",
        ),
        UniqueConstraint(
            "reseller_id", "product_id", name="uq_reseller_product_access_reseller"
        ),
        UniqueConstraint(
            "group_id", "product_id", name="uq_reseller_product_access_group"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    group_id = Column(
        Integer,
        ForeignKey(_FK_GROUPS, ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    product_id = Column(
        Integer,
        ForeignKey(_FK_PRODUCTS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    allowed = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    reseller = relationship("Reseller", back_populates="product_access")
    group = relationship("ResellerGroup", back_populates="product_access")
    product = relationship("Product")


class ResellerClientProductPermission(Base):
    __tablename__ = "reseller_client_product_permissions"
    __table_args__ = (
        UniqueConstraint(
            "reseller_id",
            "product_id",
            name="uq_reseller_client_product_permission",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id = Column(
        Integer,
        ForeignKey(_FK_PRODUCTS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    visible = Column(Boolean, nullable=False, default=True, server_default="1")
    permissions = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    reseller = relationship("Reseller", back_populates="client_product_permissions")
    product = relationship("Product")


class StockQuota(Base):
    __tablename__ = "stock_quotas"
    __table_args__ = (
        CheckConstraint(
            "(reseller_id IS NOT NULL AND group_id IS NULL) OR "
            "(reseller_id IS NULL AND group_id IS NOT NULL)",
            name="ck_stock_quotas_one_owner",
        ),
        UniqueConstraint(
            "reseller_id", "scope_type", "scope_id", name="uq_stock_quotas_reseller_scope"
        ),
        UniqueConstraint(
            "group_id", "scope_type", "scope_id", name="uq_stock_quotas_group_scope"
        ),
        CheckConstraint(
            "max_services IS NULL OR max_services >= 0",
            name="ck_stock_quotas_count_nonnegative",
        ),
        CheckConstraint(
            "max_cpu_cores IS NULL OR max_cpu_cores >= 0",
            name="ck_stock_quotas_cpu_nonnegative",
        ),
        CheckConstraint(
            "max_ram_mb IS NULL OR max_ram_mb >= 0",
            name="ck_stock_quotas_memory_nonnegative",
        ),
        CheckConstraint(
            "max_disk_gb IS NULL OR max_disk_gb >= 0",
            name="ck_stock_quotas_storage_nonnegative",
        ),
        CheckConstraint(
            "max_services IS NOT NULL OR max_cpu_cores IS NOT NULL OR "
            "max_ram_mb IS NOT NULL OR max_disk_gb IS NOT NULL",
            name="ck_stock_quotas_has_limit",
        ),
        Index("ix_stock_quotas_scope", "scope_type", "scope_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    group_id = Column(
        Integer,
        ForeignKey(_FK_GROUPS, ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    scope_type = Column(_string_enum(StockQuotaScope), nullable=False)
    scope_id = Column(Integer, nullable=False)
    max_services = Column(Integer, nullable=True)
    max_cpu_cores = Column(Integer, nullable=True)
    max_ram_mb = Column(BigInteger, nullable=True)
    max_disk_gb = Column(BigInteger, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    reseller = relationship("Reseller", back_populates="stock_quotas")
    group = relationship("ResellerGroup", back_populates="stock_quotas")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_invoices_positive_amount"),
        Index("ix_invoices_reseller_status_due", "reseller_id", "status", "due_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    invoice_number = Column(BigInteger, nullable=False, unique=True)
    # Nullable for retail client invoices (billing_account_id is the payer).
    # Reseller invoices keep reseller_id populated for B2B compatibility.
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    service_id = Column(
        Integer,
        ForeignKey(_FK_SERVICES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_accounts.id", ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    order_id = Column(Integer, nullable=True, index=True)
    purpose = Column(_string_enum(InvoicePurpose), nullable=False, index=True)
    status = Column(
        _string_enum(InvoiceStatus),
        nullable=False,
        default=InvoiceStatus.DRAFT,
        server_default=InvoiceStatus.DRAFT.value,
        index=True,
    )
    amount_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    description = Column(Text, nullable=True)
    due_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    reseller = relationship("Reseller", back_populates="invoices")
    service = relationship("Service")
    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    payments = relationship("Payment", back_populates="invoice")
    invoice_lines = relationship(
        "InvoiceLine",
        back_populates="invoice",
        cascade=_CASCADE_DELETE_ORPHAN,
        order_by="InvoiceLine.sort_order",
    )


class InvoiceSequence(Base):
    """Named transactional counters used to allocate invoice numbers."""

    __tablename__ = "invoice_sequences"

    name = Column(String(64), primary_key=True)
    next_value = Column(BigInteger, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payments_positive_amount"),
        Index("ix_payments_invoice_status", "invoice_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(
        Integer,
        ForeignKey(_FK_INVOICES, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    gateway = Column(String(64), nullable=False, index=True)
    status = Column(
        _string_enum(PaymentStatus),
        nullable=False,
        default=PaymentStatus.PENDING,
        server_default=PaymentStatus.PENDING.value,
        index=True,
    )
    amount_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    external_ref = Column(String(255), nullable=True, unique=True)
    failure_code = Column(String(128), nullable=True)
    failure_message = Column(Text, nullable=True)
    payment_metadata = Column("metadata", JSON, nullable=False, default=dict)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    refunded_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    invoice = relationship("Invoice", back_populates="payments")


class ResellerPaymentMethod(Base):
    __tablename__ = "reseller_payment_methods"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_method_ref",
            name="uq_reseller_payment_methods_provider_ref",
        ),
        CheckConstraint(
            "tier >= 0",
            name="ck_reseller_payment_methods_tier_nonnegative",
        ),
        Index(
            "ix_reseller_payment_methods_reseller_enabled",
            "reseller_id",
            "enabled",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(64), nullable=False)
    method_type = Column(String(64), nullable=False)
    provider_customer_ref = Column(String(255), nullable=True, index=True)
    provider_method_ref = Column(String(255), nullable=False)
    label = Column(String(255), nullable=True)
    brand = Column(String(64), nullable=True)
    last4 = Column(String(4), nullable=True)
    tier = Column(Integer, nullable=False, default=1, server_default="1")
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    is_default = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    reseller = relationship("Reseller", back_populates="payment_methods")


class PayPalPendingSetup(Base):
    """Tenant-bound PayPal setup token awaiting vault exchange."""

    __tablename__ = "paypal_pending_setups"
    __table_args__ = (
        Index(
            "ix_paypal_pending_setups_reseller_expires",
            "reseller_id",
            "expires_at",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    setup_token_ref = Column(String(255), nullable=False, unique=True)
    provider_customer_ref = Column(String(255), nullable=True)
    payment_method_id = Column(
        Integer,
        ForeignKey(
            "reseller_payment_methods.id", ondelete=_ON_DELETE_SET_NULL
        ),
        nullable=True,
        index=True,
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    reseller = relationship("Reseller")
    payment_method = relationship("ResellerPaymentMethod")


class GatewayWebhookEvent(Base):
    """A processed gateway event; the unique key makes webhook replay harmless."""

    __tablename__ = "gateway_webhook_events"
    __table_args__ = (
        UniqueConstraint("gateway", "event_id", name="uq_gateway_webhook_event"),
        Index("ix_gateway_webhook_created", "gateway", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    gateway = Column(String(64), nullable=False)
    event_id = Column(String(255), nullable=False)
    event_type = Column(String(255), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CreditLedgerEntry(Base):
    __tablename__ = "credit_ledger_entries"
    __table_args__ = (
        CheckConstraint("amount_cents <> 0", name="ck_credit_ledger_nonzero_amount"),
        Index(
            "ix_credit_ledger_reseller_created",
            "reseller_id",
            "created_at",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    entry_type = Column(
        _string_enum(CreditLedgerEntryType),
        nullable=False,
        index=True,
    )
    amount_cents = Column(BigInteger, nullable=False)
    balance_after_cents = Column(BigInteger, nullable=False)
    description = Column(String(512), nullable=True)
    invoice_id = Column(
        Integer,
        ForeignKey(_FK_INVOICES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    payment_id = Column(
        Integer,
        ForeignKey("payments.id", ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    service_id = Column(
        Integer,
        ForeignKey(_FK_SERVICES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    reversal_of_id = Column(
        Integer,
        ForeignKey("credit_ledger_entries.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    reference_type = Column(String(64), nullable=True)
    reference_id = Column(String(255), nullable=True)
    idempotency_key = Column(String(255), nullable=True, unique=True)
    ledger_metadata = Column("metadata", JSON, nullable=False, default=dict)
    created_by_user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    reseller = relationship("Reseller", back_populates="ledger_entries")
    invoice = relationship("Invoice")
    payment = relationship("Payment")
    service = relationship("Service")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    reversal_of = relationship(
        "CreditLedgerEntry",
        remote_side=[id],
        foreign_keys=[reversal_of_id],
        backref=backref("reversal", uselist=False),
    )


class ServiceBilling(Base):
    __tablename__ = "service_billings"
    __table_args__ = (
        CheckConstraint(
            "setup_price_cents >= 0",
            name="ck_service_billings_setup_nonnegative",
        ),
        CheckConstraint(
            "monthly_price_cents >= 0",
            name="ck_service_billings_monthly_nonnegative",
        ),
        CheckConstraint(
            "failed_attempts >= 0",
            name="ck_service_billings_failures_nonnegative",
        ),
        CheckConstraint(
            "billing_anchor_day IS NULL OR "
            "(billing_anchor_day >= 1 AND billing_anchor_day <= 31)",
            name="ck_service_billings_anchor_day",
        ),
        Index(
            "ix_service_billings_status_next_charge",
            "status",
            "next_charge_at",
        ),
        Index(
            "ix_service_billings_reseller_status",
            "reseller_id",
            "status",
        ),
        Index(
            "ix_service_billings_billing_account_status",
            "billing_account_id",
            "status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(
        Integer,
        ForeignKey(_FK_SERVICES, ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_accounts.id", ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    product_id = Column(
        Integer,
        ForeignKey(_FK_PRODUCTS, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    setup_price_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    monthly_price_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    next_charge_at = Column(DateTime(timezone=True), nullable=True, index=True)
    billing_anchor_day = Column(Integer, nullable=True)
    status = Column(
        _string_enum(ServiceBillingStatus),
        nullable=False,
        default=ServiceBillingStatus.ACTIVE,
        server_default=ServiceBillingStatus.ACTIVE.value,
        index=True,
    )
    failed_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_failure_code = Column(String(128), nullable=True)
    last_failure_message = Column(Text, nullable=True)
    grace_until = Column(DateTime(timezone=True), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_charged_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    service = relationship("Service")
    reseller = relationship("Reseller", back_populates="service_billings")
    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    product = relationship("Product")
    cycles = relationship(
        "BillingCycle",
        back_populates="service_billing",
        cascade=_CASCADE_DELETE_ORPHAN,
        order_by="BillingCycle.due_at",
    )


class BillingCycle(Base):
    """One immutable monthly due instant and its single reusable invoice."""

    __tablename__ = "billing_cycles"
    __table_args__ = (
        UniqueConstraint(
            "service_billing_id",
            "due_at",
            name="uq_billing_cycles_billing_due",
        ),
        CheckConstraint(
            "amount_cents >= 0",
            name="ck_billing_cycles_nonnegative_amount",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_billing_cycles_attempts_nonnegative",
        ),
        Index("ix_billing_cycles_state_retry", "state", "next_retry_at"),
        Index("ix_billing_cycles_claim", "claim_expires_at", "claim_token"),
    )

    id = Column(Integer, primary_key=True, index=True)
    service_billing_id = Column(
        Integer,
        ForeignKey("service_billings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invoice_id = Column(
        Integer,
        ForeignKey(_FK_INVOICES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        unique=True,
        index=True,
    )
    due_at = Column(DateTime(timezone=True), nullable=False, index=True)
    amount_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    state = Column(
        _string_enum(BillingCycleState),
        nullable=False,
        default=BillingCycleState.DUE,
        server_default=BillingCycleState.DUE.value,
        index=True,
    )
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    grace_until = Column(DateTime(timezone=True), nullable=True)
    failure_code = Column(String(128), nullable=True)
    failure_message = Column(Text, nullable=True)
    claim_token = Column(String(128), nullable=True)
    claim_expires_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    service_billing = relationship("ServiceBilling", back_populates="cycles")
    invoice = relationship("Invoice")


class RecurringBillingLease(Base):
    """Durable singleton leases shared by recurring billing worker replicas."""

    __tablename__ = "recurring_billing_leases"

    name = Column(String(64), primary_key=True)
    owner = Column(String(128), nullable=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class NotificationOutbox(Base):
    """Durable, deduplicated notification waiting for SMTP delivery."""

    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint(
            "attempts >= 0",
            name="ck_notification_outbox_attempts_nonnegative",
        ),
        Index(
            "ix_notification_outbox_status_attempt",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_notification_outbox_claim",
            "claim_expires_at",
            "claim_token",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    recipient = Column(String(320), nullable=False, index=True)
    event = Column(String(64), nullable=False, index=True)
    template = Column(String(64), nullable=False)
    data = Column(JSON, nullable=False, default=dict)
    status = Column(
        _string_enum(NotificationOutboxStatus),
        nullable=False,
        default=NotificationOutboxStatus.QUEUED,
        server_default=NotificationOutboxStatus.QUEUED.value,
        index=True,
    )
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, index=True)
    error = Column(Text, nullable=True)
    claim_token = Column(String(128), nullable=True)
    claim_expires_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    reseller = relationship("Reseller")


class ResellerProvisioningRequest(Base):
    """Tenant-scoped durable mapping for create idempotency."""

    __tablename__ = "reseller_provisioning_requests"
    __table_args__ = (
        UniqueConstraint(
            "reseller_id",
            "idempotency_key",
            name="uq_reseller_provisioning_request_key",
        ),
        Index(
            "ix_reseller_provisioning_request_status",
            "reseller_id",
            "status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(String(255), nullable=False)
    request_hash = Column(String(64), nullable=False)
    invoice_id = Column(
        Integer,
        ForeignKey(_FK_INVOICES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    service_id = Column(
        Integer,
        ForeignKey(_FK_SERVICES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    status = Column(
        _string_enum(ResellerProvisioningRequestStatus),
        nullable=False,
        default=ResellerProvisioningRequestStatus.PROCESSING,
        server_default=ResellerProvisioningRequestStatus.PROCESSING.value,
        index=True,
    )
    error_message = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    reseller = relationship("Reseller", back_populates="provisioning_requests")
    invoice = relationship("Invoice")
    service = relationship("Service")
