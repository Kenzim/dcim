"""service_bare_metal + service_vm extension tables; slim services; server_activity service_id

Revision ID: svc_ext_split
Revises: svc_vm_place
Create Date: 2026-03-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "svc_ext_split"
down_revision: Union[str, Sequence[str], None] = "svc_vm_place"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return column in [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    dialect = conn.dialect.name

    if not _has_table(inspector, "services"):
        return

    if not _has_table(inspector, "service_bare_metal"):
        op.create_table(
            "service_bare_metal",
            sa.Column("service_id", sa.Integer(), nullable=False),
            sa.Column("server_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
            sa.PrimaryKeyConstraint("service_id"),
        )
        op.create_index("ix_service_bare_metal_server_id", "service_bare_metal", ["server_id"])

    if not _has_table(inspector, "service_vm"):
        op.create_table(
            "service_vm",
            sa.Column("service_id", sa.Integer(), nullable=False),
            sa.Column("proxmox_cluster_id", sa.Integer(), nullable=True),
            sa.Column("proxmox_node_name", sa.String(255), nullable=True),
            sa.Column("proxmox_vmid", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["proxmox_cluster_id"], ["proxmox_clusters.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("service_id"),
        )
        op.create_index("ix_service_vm_proxmox_cluster_id", "service_vm", ["proxmox_cluster_id"])

    cols = [c["name"] for c in inspector.get_columns("services")]
    if "server_id" in cols:
        op.execute(
            sa.text(
                """
                INSERT INTO service_bare_metal (service_id, server_id)
                SELECT id, server_id FROM services
                WHERE service_type IS NULL OR service_type != 'vm'
                """
            )
        )
        op.execute(
            sa.text(
                """
                INSERT INTO service_vm (service_id, proxmox_cluster_id, proxmox_node_name, proxmox_vmid)
                SELECT id, proxmox_cluster_id, proxmox_node_name, proxmox_vmid
                FROM services WHERE service_type = 'vm'
                """
            )
        )

        for fk in inspector.get_foreign_keys("services"):
            if "server_id" in fk.get("constrained_columns", []):
                op.drop_constraint(fk["name"], "services", type_="foreignkey")
        try:
            op.drop_index("ix_services_server_id", table_name="services")
        except Exception:
            pass

        op.drop_column("services", "server_id")

        inspector = sa.inspect(conn)
        if _has_column(inspector, "services", "proxmox_cluster_id"):
            try:
                op.drop_index("ix_services_proxmox_cluster_id", table_name="services")
            except Exception:
                pass
            try:
                op.drop_constraint("fk_services_proxmox_cluster_id", "services", type_="foreignkey")
            except Exception:
                for fk in sa.inspect(conn).get_foreign_keys("services"):
                    if "proxmox_cluster_id" in fk.get("constrained_columns", []):
                        op.drop_constraint(fk["name"], "services", type_="foreignkey")
            op.drop_column("services", "proxmox_cluster_id")
        if _has_column(inspector, "services", "proxmox_node_name"):
            op.drop_column("services", "proxmox_node_name")
        if _has_column(inspector, "services", "proxmox_vmid"):
            op.drop_column("services", "proxmox_vmid")

    inspector = sa.inspect(conn)
    if _has_table(inspector, "server_activity"):
        if not _has_column(inspector, "server_activity", "service_id"):
            op.add_column("server_activity", sa.Column("service_id", sa.Integer(), nullable=True))
            op.create_foreign_key(
                "fk_server_activity_service_id",
                "server_activity",
                "services",
                ["service_id"],
                ["id"],
            )
            op.create_index("ix_server_activity_service_id", "server_activity", ["service_id"])
        if _has_column(inspector, "server_activity", "server_id"):
            op.alter_column("server_activity", "server_id", existing_type=sa.Integer(), nullable=True)

        if dialect == "postgresql":
            op.create_check_constraint(
                "ck_server_activity_server_xor_service",
                "server_activity",
                "(server_id IS NOT NULL AND service_id IS NULL) OR (server_id IS NULL AND service_id IS NOT NULL)",
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    dialect = conn.dialect.name

    if _has_table(inspector, "server_activity"):
        if dialect == "postgresql":
            try:
                op.drop_constraint("ck_server_activity_server_xor_service", "server_activity", type_="check")
            except Exception:
                pass
        if _has_column(inspector, "server_activity", "service_id"):
            try:
                op.drop_index("ix_server_activity_service_id", table_name="server_activity")
            except Exception:
                pass
            try:
                op.drop_constraint("fk_server_activity_service_id", "server_activity", type_="foreignkey")
            except Exception:
                pass
            op.drop_column("server_activity", "service_id")
        if _has_column(inspector, "server_activity", "server_id"):
            op.alter_column("server_activity", "server_id", existing_type=sa.Integer(), nullable=False)

    if not _has_table(inspector, "services"):
        return

    cols = [c["name"] for c in inspector.get_columns("services")]
    if "server_id" not in cols:
        op.add_column("services", sa.Column("server_id", sa.Integer(), nullable=True))
        op.execute(
            sa.text(
                """
                UPDATE services SET server_id = (
                    SELECT server_id FROM service_bare_metal WHERE service_bare_metal.service_id = services.id
                )
                """
            )
        )
        op.execute(
            sa.text(
                """
                UPDATE services SET
                    proxmox_cluster_id = (SELECT proxmox_cluster_id FROM service_vm sv WHERE sv.service_id = services.id),
                    proxmox_node_name = (SELECT proxmox_node_name FROM service_vm sv WHERE sv.service_id = services.id),
                    proxmox_vmid = (SELECT proxmox_vmid FROM service_vm sv WHERE sv.service_id = services.id)
                WHERE service_type = 'vm'
                """
            )
        )
        op.alter_column("services", "server_id", existing_type=sa.Integer(), nullable=False)
        op.create_foreign_key(None, "services", "servers", ["server_id"], ["id"])
        op.create_index("ix_services_server_id", "services", ["server_id"])
        try:
            op.add_column(
                "services",
                sa.Column("proxmox_cluster_id", sa.Integer(), nullable=True),
            )
            op.create_foreign_key(
                "fk_services_proxmox_cluster_id",
                "services",
                "proxmox_clusters",
                ["proxmox_cluster_id"],
                ["id"],
                ondelete="SET NULL",
            )
            op.create_index("ix_services_proxmox_cluster_id", "services", ["proxmox_cluster_id"])
        except Exception:
            pass
        try:
            op.add_column("services", sa.Column("proxmox_node_name", sa.String(255), nullable=True))
            op.add_column("services", sa.Column("proxmox_vmid", sa.Integer(), nullable=True))
        except Exception:
            pass

    if _has_table(inspector, "service_vm"):
        op.drop_table("service_vm")
    if _has_table(inspector, "service_bare_metal"):
        op.drop_table("service_bare_metal")
