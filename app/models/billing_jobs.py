"""Billing automation tables of phase 2 (docs/billing-phase2-design.md 1.10).

Job modes live in ``billing_job_states``, never in the settings store, and
change only through the job mode endpoint (design 9.2).
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.reseller import _string_enum

# SQLite only auto-increments an INTEGER PRIMARY KEY.
BIGINT_ID = BigInteger().with_variant(Integer(), "sqlite")

_ON_DELETE_SET_NULL = "SET NULL"

# The jobs of design 9.1; each has one billing_job_states row.
BILLING_JOB_NAMES: tuple[str, ...] = (
    "generate_invoices_ahead",
    "invoice_reminders",
    "late_fees",
    "nonpayment_suspension",
    "nonpayment_termination",
    "cancellations",
    "tax_id_recheck",
    "tax_exchange_rates_fetch",
)


class BillingJobMode(str, enum.Enum):
    OFF = "off"
    DRY_RUN = "dry_run"
    LIVE = "live"


class BillingJobRunMode(str, enum.Enum):
    DRY_RUN = "dry_run"
    LIVE = "live"


class BillingJobTrigger(str, enum.Enum):
    SCHEDULE = "schedule"
    ADMIN = "admin"


class BillingJobRunStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    ABORTED = "aborted"
    # A live run that found the job's live preconditions unmet and acted on nothing.
    SKIPPED = "skipped"


class BillingJobItemOutcome(str, enum.Enum):
    PLANNED = "planned"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    ALREADY_DONE = "already_done"


class BillingJobActionStatus(str, enum.Enum):
    CLAIMED = "claimed"
    DONE = "done"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class NonpaymentPolicyScope(str, enum.Enum):
    RETAIL_DEFAULT = "retail_default"
    RESELLER_DEFAULT = "reseller_default"
    PRODUCT = "product"


NONPAYMENT_POLICY_SCOPE_CHECK = (
    "(scope = 'product' AND product_id > 0) OR (scope <> 'product' AND product_id = 0)"
)


class UnpaidInvoiceAction(str, enum.Enum):
    KEEP = "keep"
    CANCEL = "cancel"


class CancellationType(str, enum.Enum):
    END_OF_PERIOD = "end_of_period"
    IMMEDIATE = "immediate"


class CancellationSource(str, enum.Enum):
    CLIENT = "client"
    ADMIN = "admin"
    RESELLER = "reseller"


class CancellationStatus(str, enum.Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"
    FAILED = "failed"


class CancellationInvoiceAction(str, enum.Enum):
    CANCEL_UNPAID = "cancel_unpaid"
    KEEP = "keep"


class BillingJobState(Base):
    __tablename__ = "billing_job_states"

    job = Column(String(64), primary_key=True)
    mode = Column(
        _string_enum(BillingJobMode, length=16),
        nullable=False,
        default=BillingJobMode.OFF,
        server_default=BillingJobMode.OFF.value,
    )
    dry_run_since = Column(DateTime(timezone=True), nullable=True)
    live_since = Column(DateTime(timezone=True), nullable=True)
    config_hash = Column(String(64), nullable=True)
    config_since = Column(DateTime(timezone=True), nullable=True)
    last_run_id = Column(
        Integer,
        ForeignKey("billing_job_runs.id", ondelete=_ON_DELETE_SET_NULL, name="fk_billing_job_states_last_run_id"),
        nullable=True,
    )
    changed_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete=_ON_DELETE_SET_NULL, name="fk_billing_job_states_changed_by_user_id"),
        nullable=True,
    )
    changed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BillingJobRun(Base):
    __tablename__ = "billing_job_runs"
    __table_args__ = (Index("ix_billing_job_runs_job_started", "job", "started_at"),)

    id = Column(Integer, primary_key=True)
    job = Column(String(64), nullable=False)
    mode = Column(_string_enum(BillingJobRunMode, length=16), nullable=False)
    trigger = Column(_string_enum(BillingJobTrigger, length=16), nullable=False)
    owner = Column(String(128), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(
        _string_enum(BillingJobRunStatus, length=16),
        nullable=False,
        default=BillingJobRunStatus.RUNNING,
        server_default=BillingJobRunStatus.RUNNING.value,
    )
    now_at = Column(DateTime(timezone=True), nullable=False)
    watermark_at = Column(DateTime(timezone=True), nullable=True)
    counts = Column(JSON, nullable=True)
    error = Column(String(1000), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BillingJobRunItem(Base):
    """What a run planned or did for one subject; kept ``billing_job_run_retention_days``."""

    __tablename__ = "billing_job_run_items"
    __table_args__ = (Index("ix_billing_job_run_items_run_outcome", "run_id", "outcome"),)

    id = Column(BIGINT_ID, primary_key=True)
    run_id = Column(
        Integer,
        ForeignKey("billing_job_runs.id", ondelete="CASCADE", name="fk_billing_job_run_items_run_id"),
        nullable=False,
    )
    subject_key = Column(String(128), nullable=False)
    action = Column(String(64), nullable=False)
    outcome = Column(_string_enum(BillingJobItemOutcome, length=16), nullable=False)
    detail = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BillingJobAction(Base):
    """Claim and idempotency record of one live action (design 9.3).

    Kept after its run is purged by retention, so ``run_id`` becomes NULL then.
    """

    __tablename__ = "billing_job_actions"
    __table_args__ = (
        UniqueConstraint("job", "subject_key", name="uq_billing_job_actions_job_subject"),
        Index("ix_billing_job_actions_status_claimed", "status", "claimed_at"),
    )

    id = Column(BIGINT_ID, primary_key=True)
    job = Column(String(64), nullable=False)
    subject_key = Column(String(128), nullable=False)
    run_id = Column(
        Integer,
        ForeignKey("billing_job_runs.id", ondelete=_ON_DELETE_SET_NULL, name="fk_billing_job_actions_run_id"),
        nullable=True,
        index=True,
    )
    status = Column(
        _string_enum(BillingJobActionStatus, length=16),
        nullable=False,
        default=BillingJobActionStatus.CLAIMED,
        server_default=BillingJobActionStatus.CLAIMED.value,
    )
    claimed_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    detail = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BillingNonpaymentPolicy(Base):
    """Suspension and termination days of one track or product (B7).

    ``product_id`` is 0 for the two default rows, so the unique key works on
    MariaDB (whose unique indexes do not treat NULLs as equal); it therefore
    carries no foreign key, as ``email_templates.product_scope``. The check
    constraint ties the two together: a ``product`` row names a product, any
    other scope has ``product_id`` 0.
    """

    __tablename__ = "billing_nonpayment_policies"
    __table_args__ = (
        UniqueConstraint("scope", "product_id", name="uq_billing_nonpayment_policies_scope_product"),
        CheckConstraint(NONPAYMENT_POLICY_SCOPE_CHECK, name="ck_billing_nonpayment_policies_scope_product"),
    )

    id = Column(Integer, primary_key=True)
    scope = Column(_string_enum(NonpaymentPolicyScope, length=32), nullable=False)
    product_id = Column(Integer, nullable=False, default=0, server_default="0")
    suspend_after_days = Column(SmallInteger, nullable=True)
    terminate_after_days = Column(SmallInteger, nullable=True)
    termination_notice_days = Column(SmallInteger, nullable=False, default=7, server_default="7")
    unpaid_invoice_action_on_terminate = Column(
        _string_enum(UnpaidInvoiceAction, length=16),
        nullable=False,
        default=UnpaidInvoiceAction.KEEP,
        server_default=UnpaidInvoiceAction.KEEP.value,
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CancellationRequest(Base):
    __tablename__ = "cancellation_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_cancellation_requests_idempotency_key"),
        Index("ix_cancellation_requests_status_effective", "status", "effective_at"),
    )

    id = Column(Integer, primary_key=True)
    service_id = Column(
        Integer,
        ForeignKey("services.id", ondelete="RESTRICT", name="fk_cancellation_requests_service_id"),
        nullable=False,
        index=True,
    )
    service_billing_id = Column(
        Integer,
        ForeignKey(
            "service_billings.id", ondelete=_ON_DELETE_SET_NULL, name="fk_cancellation_requests_service_billing_id"
        ),
        nullable=True,
        index=True,
    )
    billing_account_id = Column(
        Integer,
        ForeignKey(
            "billing_accounts.id", ondelete=_ON_DELETE_SET_NULL, name="fk_cancellation_requests_billing_account_id"
        ),
        nullable=True,
        index=True,
    )
    reseller_id = Column(
        Integer,
        ForeignKey("resellers.id", ondelete=_ON_DELETE_SET_NULL, name="fk_cancellation_requests_reseller_id"),
        nullable=True,
        index=True,
    )
    type = Column(_string_enum(CancellationType, length=16), nullable=False)
    reason = Column(String(1000), nullable=True)
    requested_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete=_ON_DELETE_SET_NULL, name="fk_cancellation_requests_requested_by_user_id"),
        nullable=True,
    )
    source = Column(_string_enum(CancellationSource, length=16), nullable=False)
    status = Column(
        _string_enum(CancellationStatus, length=16),
        nullable=False,
        default=CancellationStatus.PENDING,
        server_default=CancellationStatus.PENDING.value,
    )
    effective_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    invoice_action = Column(
        _string_enum(CancellationInvoiceAction, length=16),
        nullable=False,
        default=CancellationInvoiceAction.CANCEL_UNPAID,
        server_default=CancellationInvoiceAction.CANCEL_UNPAID.value,
    )
    idempotency_key = Column(String(128), nullable=False)
    failure = Column(String(1000), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
