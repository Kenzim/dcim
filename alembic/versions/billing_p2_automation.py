"""Billing phase 2: automation tables

Creates ``billing_job_runs``, ``billing_job_states`` (one row per job of
docs/billing-phase2-design.md 9.1, every job in mode ``off``),
``billing_job_run_items``, ``billing_job_actions``,
``billing_nonpayment_policies`` (the ``retail_default`` and
``reseller_default`` rows with every day column NULL, which is today's
behaviour) and ``cancellation_requests`` (design 1.10). New tables only.

``billing_nonpayment_policies.product_id`` is 0 on the two default rows so the
unique key ``(scope, product_id)`` holds on MariaDB, whose unique indexes do
not treat NULLs as equal; for that reason it has no foreign key.

Revision ID: billing_p2_automation
Revises: billing_p2_tax_seed
Create Date: 2026-11-22 12:20:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p2_automation"
down_revision: Union[str, Sequence[str], None] = "billing_p2_tax_seed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

JOBS = (
    "generate_invoices_ahead",
    "invoice_reminders",
    "late_fees",
    "nonpayment_suspension",
    "nonpayment_termination",
    "cancellations",
    "tax_id_recheck",
)
POLICY_SCOPES = ("retail_default", "reseller_default")

TABLES = (
    "cancellation_requests",
    "billing_nonpayment_policies",
    "billing_job_actions",
    "billing_job_run_items",
    "billing_job_states",
    "billing_job_runs",
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "billing_job_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("now_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(1000), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_billing_job_runs_job_started", "billing_job_runs", ["job", "started_at"])

    states = op.create_table(
        "billing_job_states",
        sa.Column("job", sa.String(64), primary_key=True),
        sa.Column("mode", sa.String(16), nullable=False, server_default="off"),
        sa.Column("dry_run_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("live_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config_hash", sa.String(64), nullable=True),
        sa.Column("config_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_run_id",
            sa.Integer(),
            sa.ForeignKey("billing_job_runs.id", ondelete="SET NULL", name="fk_billing_job_states_last_run_id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_billing_job_states_changed_by_user_id"),
            nullable=True,
        ),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )

    op.create_table(
        "billing_job_run_items",
        sa.Column("id", BIGINT_ID, primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("billing_job_runs.id", ondelete="CASCADE", name="fk_billing_job_run_items_run_id"),
            nullable=False,
        ),
        sa.Column("subject_key", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_billing_job_run_items_run_outcome", "billing_job_run_items", ["run_id", "outcome"])

    op.create_table(
        "billing_job_actions",
        sa.Column("id", BIGINT_ID, primary_key=True),
        sa.Column("job", sa.String(64), nullable=False),
        sa.Column("subject_key", sa.String(128), nullable=False),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("billing_job_runs.id", ondelete="SET NULL", name="fk_billing_job_actions_run_id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="claimed"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("job", "subject_key", name="uq_billing_job_actions_job_subject"),
    )
    op.create_index("ix_billing_job_actions_run_id", "billing_job_actions", ["run_id"])
    op.create_index("ix_billing_job_actions_status_claimed", "billing_job_actions", ["status", "claimed_at"])

    policies = op.create_table(
        "billing_nonpayment_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suspend_after_days", sa.SmallInteger(), nullable=True),
        sa.Column("terminate_after_days", sa.SmallInteger(), nullable=True),
        sa.Column("termination_notice_days", sa.SmallInteger(), nullable=False, server_default="7"),
        sa.Column(
            "unpaid_invoice_action_on_terminate", sa.String(16), nullable=False, server_default="keep"
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("scope", "product_id", name="uq_billing_nonpayment_policies_scope_product"),
    )

    op.create_table(
        "cancellation_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="RESTRICT", name="fk_cancellation_requests_service_id"),
            nullable=False,
        ),
        sa.Column(
            "service_billing_id",
            sa.Integer(),
            sa.ForeignKey(
                "service_billings.id",
                ondelete="SET NULL",
                name="fk_cancellation_requests_service_billing_id",
            ),
            nullable=True,
        ),
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey(
                "billing_accounts.id",
                ondelete="SET NULL",
                name="fk_cancellation_requests_billing_account_id",
            ),
            nullable=True,
        ),
        sa.Column(
            "reseller_id",
            sa.Integer(),
            sa.ForeignKey("resellers.id", ondelete="SET NULL", name="fk_cancellation_requests_reseller_id"),
            nullable=True,
        ),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=True),
        sa.Column(
            "requested_by_user_id",
            sa.Integer(),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_cancellation_requests_requested_by_user_id"
            ),
            nullable=True,
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invoice_action", sa.String(16), nullable=False, server_default="cancel_unpaid"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("failure", sa.String(1000), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("idempotency_key", name="uq_cancellation_requests_idempotency_key"),
    )
    for column in ("service_id", "service_billing_id", "billing_account_id", "reseller_id"):
        op.create_index(f"ix_cancellation_requests_{column}", "cancellation_requests", [column])
    op.create_index(
        "ix_cancellation_requests_status_effective", "cancellation_requests", ["status", "effective_at"]
    )

    op.bulk_insert(states, [{"job": job, "mode": "off"} for job in JOBS])
    op.bulk_insert(
        policies,
        [
            {
                "scope": scope,
                "product_id": 0,
                "suspend_after_days": None,
                "terminate_after_days": None,
                "termination_notice_days": 7,
                "unpaid_invoice_action_on_terminate": "keep",
                "enabled": True,
            }
            for scope in POLICY_SCOPES
        ],
    )


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
