"""service ownership links, vm guest lifecycle, and cluster vmid ranges

Revision ID: svc_owner_vm_lifecycle
Revises: svc_vm_vm_ip_alloc
Create Date: 2026-03-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "svc_owner_vm_lifecycle"
down_revision: Union[str, Sequence[str], None] = "svc_vm_vm_ip_alloc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return column in [c["name"] for c in inspector.get_columns(table)]


def _has_index(inspector, table: str, index_name: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return index_name in [idx["name"] for idx in inspector.get_indexes(table)]


def _has_unique_constraint(inspector, table: str, name: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return name in [uc["name"] for uc in inspector.get_unique_constraints(table)]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_table(inspector, "services"):
        if not _has_column(inspector, "services", "owner_user_id"):
            op.add_column("services", sa.Column("owner_user_id", sa.Integer(), nullable=True))
            op.create_foreign_key(
                "fk_services_owner_user_id",
                "services",
                "users",
                ["owner_user_id"],
                ["id"],
                ondelete="SET NULL",
            )
            op.create_index("ix_services_owner_user_id", "services", ["owner_user_id"])

    inspector = sa.inspect(conn)
    if _has_table(inspector, "service_vm"):
        if not _has_column(inspector, "service_vm", "guest_state"):
            op.add_column(
                "service_vm",
                sa.Column("guest_state", sa.String(length=32), nullable=False, server_default="unprovisioned"),
            )
            op.create_index("ix_service_vm_guest_state", "service_vm", ["guest_state"])
        if not _has_column(inspector, "service_vm", "guest_last_error"):
            op.add_column("service_vm", sa.Column("guest_last_error", sa.Text(), nullable=True))
        if not _has_column(inspector, "service_vm", "guest_last_transition_at"):
            op.add_column(
                "service_vm",
                sa.Column("guest_last_transition_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            )

    inspector = sa.inspect(conn)
    if _has_table(inspector, "service_vm") and not _has_unique_constraint(
        inspector, "service_vm", "uq_service_vm_cluster_vmid"
    ):
        op.create_unique_constraint(
            "uq_service_vm_cluster_vmid",
            "service_vm",
            ["proxmox_cluster_id", "proxmox_vmid"],
        )

    inspector = sa.inspect(conn)
    if _has_table(inspector, "proxmox_clusters"):
        if not _has_column(inspector, "proxmox_clusters", "vmid_min"):
            op.add_column("proxmox_clusters", sa.Column("vmid_min", sa.Integer(), nullable=True))
        if not _has_column(inspector, "proxmox_clusters", "vmid_max"):
            op.add_column("proxmox_clusters", sa.Column("vmid_max", sa.Integer(), nullable=True))

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "user_external_identity_links"):
        op.create_table(
            "user_external_identity_links",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("external_user_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["external_user_id"], ["external_users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("user_id", "external_user_id", name="uq_user_external_identity_link_pair"),
            sa.UniqueConstraint("external_user_id", name="uq_user_external_identity_external_unique"),
        )
        op.create_index("ix_user_external_identity_links_id", "user_external_identity_links", ["id"])
        op.create_index("ix_user_external_identity_links_user_id", "user_external_identity_links", ["user_id"])
        op.create_index(
            "ix_user_external_identity_links_external_user_id",
            "user_external_identity_links",
            ["external_user_id"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_table(inspector, "user_external_identity_links"):
        op.drop_table("user_external_identity_links")

    inspector = sa.inspect(conn)
    if _has_table(inspector, "proxmox_clusters"):
        if _has_column(inspector, "proxmox_clusters", "vmid_max"):
            op.drop_column("proxmox_clusters", "vmid_max")
        if _has_column(inspector, "proxmox_clusters", "vmid_min"):
            op.drop_column("proxmox_clusters", "vmid_min")

    inspector = sa.inspect(conn)
    if _has_table(inspector, "service_vm"):
        if _has_unique_constraint(inspector, "service_vm", "uq_service_vm_cluster_vmid"):
            op.drop_constraint("uq_service_vm_cluster_vmid", "service_vm", type_="unique")
        if _has_column(inspector, "service_vm", "guest_last_transition_at"):
            op.drop_column("service_vm", "guest_last_transition_at")
        if _has_column(inspector, "service_vm", "guest_last_error"):
            op.drop_column("service_vm", "guest_last_error")
        if _has_index(inspector, "service_vm", "ix_service_vm_guest_state"):
            op.drop_index("ix_service_vm_guest_state", table_name="service_vm")
        if _has_column(inspector, "service_vm", "guest_state"):
            op.drop_column("service_vm", "guest_state")

    inspector = sa.inspect(conn)
    if _has_table(inspector, "services"):
        if _has_index(inspector, "services", "ix_services_owner_user_id"):
            op.drop_index("ix_services_owner_user_id", table_name="services")
        if _has_column(inspector, "services", "owner_user_id"):
            try:
                op.drop_constraint("fk_services_owner_user_id", "services", type_="foreignkey")
            except Exception:
                pass
            op.drop_column("services", "owner_user_id")
