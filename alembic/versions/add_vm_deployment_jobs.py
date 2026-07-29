"""add vm deployment jobs + steps tables

Revision ID: add_vm_deploy_jobs
Revises: vm_ip_alloc_tag
Create Date: 2026-07-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_vm_deploy_jobs"
down_revision: Union[str, Sequence[str], None] = "vm_ip_alloc_tag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JOB_STATUSES = ("queued", "running", "waiting", "succeeded", "failed", "cancelled")
STEP_STATUSES = ("pending", "waiting", "running", "succeeded", "skipped", "failed")


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "vm_deployment_jobs"):
        op.create_table(
            "vm_deployment_jobs",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("service_id", sa.Integer(), nullable=False),
            sa.Column("strategy_name", sa.String(length=128), nullable=False),
            sa.Column(
                "status",
                sa.Enum(*JOB_STATUSES, native_enum=False),
                nullable=False,
                server_default="queued",
            ),
            sa.Column("current_step_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("locked_by", sa.String(length=128), nullable=True),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_vm_deployment_jobs_id", "vm_deployment_jobs", ["id"])
        op.create_index("ix_vm_deployment_jobs_service_id", "vm_deployment_jobs", ["service_id"])
        op.create_index("ix_vm_deployment_jobs_status", "vm_deployment_jobs", ["status"])
        op.create_index(
            "ix_vm_deployment_jobs_status_next_run",
            "vm_deployment_jobs",
            ["status", "next_run_at"],
        )
        op.create_index(
            "ix_vm_deployment_jobs_service_created",
            "vm_deployment_jobs",
            ["service_id", "created_at"],
        )

    if not _has_table(inspector, "vm_deployment_job_steps"):
        op.create_table(
            "vm_deployment_job_steps",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column(
                "status",
                sa.Enum(*STEP_STATUSES, native_enum=False),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("detail", sa.JSON(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["job_id"], ["vm_deployment_jobs.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_vm_deployment_job_steps_id", "vm_deployment_job_steps", ["id"])
        op.create_index("ix_vm_deployment_job_steps_job_id", "vm_deployment_job_steps", ["job_id"])
        op.create_index(
            "ix_vm_deployment_job_steps_job_position",
            "vm_deployment_job_steps",
            ["job_id", "position"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "vm_deployment_job_steps"):
        op.drop_table("vm_deployment_job_steps")
    if _has_table(inspector, "vm_deployment_jobs"):
        op.drop_table("vm_deployment_jobs")
