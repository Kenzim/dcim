"""add vm and proxy platform foundation tables

Revision ID: add_vm_proxy_platform_foundation
Revises: add_server_capabilities_table
Create Date: 2026-03-20 11:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "add_vm_proxy_platform_foundation"
down_revision: Union[str, Sequence[str], None] = "add_server_capabilities_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    if not _has_table(inspector, table_name):
        return False
    return column_name in [c["name"] for c in inspector.get_columns(table_name)]


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    if not _has_table(inspector, table_name):
        return False
    return index_name in [idx["name"] for idx in inspector.get_indexes(table_name)]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_table(inspector, "services"):
        if not _has_column(inspector, "services", "service_type"):
            op.add_column(
                "services",
                sa.Column("service_type", sa.String(length=32), nullable=False, server_default="bare_metal"),
            )
        if not _has_column(inspector, "services", "product_code"):
            op.add_column("services", sa.Column("product_code", sa.String(length=128), nullable=True))
        if not _has_column(inspector, "services", "os_code"):
            op.add_column("services", sa.Column("os_code", sa.String(length=128), nullable=True))
        if not _has_column(inspector, "services", "product_snapshot"):
            op.add_column("services", sa.Column("product_snapshot", sa.JSON(), nullable=True))

        inspector = sa.inspect(conn)
        if not _has_index(inspector, "services", "ix_services_service_type"):
            op.create_index("ix_services_service_type", "services", ["service_type"])
        if not _has_index(inspector, "services", "ix_services_product_code"):
            op.create_index("ix_services_product_code", "services", ["product_code"])
        if not _has_index(inspector, "services", "ix_services_os_code"):
            op.create_index("ix_services_os_code", "services", ["os_code"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "product_families"):
        op.create_table(
            "product_families",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("code", sa.String(length=128), nullable=False),
            sa.Column("service_type", sa.String(length=32), nullable=False),
            sa.Column("provisioning_backend", sa.String(length=64), nullable=True),
            sa.Column("defaults", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("constraints", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("code", name="uq_product_families_code"),
        )
        op.create_index("ix_product_families_id", "product_families", ["id"])
        op.create_index("ix_product_families_code", "product_families", ["code"])
        op.create_index("ix_product_families_service_type", "product_families", ["service_type"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "products"):
        op.create_table(
            "products",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("family_id", sa.Integer(), sa.ForeignKey("product_families.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("code", sa.String(length=128), nullable=False),
            sa.Column("overrides", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("code", name="uq_products_code"),
            sa.UniqueConstraint("family_id", "code", name="uq_products_family_code"),
        )
        op.create_index("ix_products_id", "products", ["id"])
        op.create_index("ix_products_family_id", "products", ["family_id"])
        op.create_index("ix_products_code", "products", ["code"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "os_profiles"):
        op.create_table(
            "os_profiles",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("code", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("os_family", sa.String(length=64), nullable=False),
            sa.Column("strategy_name", sa.String(length=128), nullable=True),
            sa.Column("strategy_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("code", name="uq_os_profiles_code"),
        )
        op.create_index("ix_os_profiles_id", "os_profiles", ["id"])
        op.create_index("ix_os_profiles_code", "os_profiles", ["code"])
        op.create_index("ix_os_profiles_os_family", "os_profiles", ["os_family"])
        op.create_index("ix_os_profiles_strategy_name", "os_profiles", ["strategy_name"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "product_family_os_profiles"):
        op.create_table(
            "product_family_os_profiles",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("family_id", sa.Integer(), sa.ForeignKey("product_families.id", ondelete="CASCADE"), nullable=False),
            sa.Column("os_profile_id", sa.Integer(), sa.ForeignKey("os_profiles.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("family_id", "os_profile_id", name="uq_family_os_profile"),
        )
        op.create_index("ix_product_family_os_profiles_id", "product_family_os_profiles", ["id"])
        op.create_index("ix_product_family_os_profiles_family_id", "product_family_os_profiles", ["family_id"])
        op.create_index("ix_product_family_os_profiles_os_profile_id", "product_family_os_profiles", ["os_profile_id"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "proxmox_clusters"):
        op.create_table(
            "proxmox_clusters",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("api_url", sa.String(length=512), nullable=False),
            sa.Column("username", sa.String(length=255), nullable=False),
            sa.Column("password", sa.String(length=255), nullable=False),
            sa.Column("verify_ssl", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index("ix_proxmox_clusters_id", "proxmox_clusters", ["id"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "proxmox_nodes"):
        op.create_table(
            "proxmox_nodes",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("proxmox_clusters.id", ondelete="CASCADE"), nullable=False),
            sa.Column("node_name", sa.String(length=255), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index("ix_proxmox_nodes_id", "proxmox_nodes", ["id"])
        op.create_index("ix_proxmox_nodes_cluster_id", "proxmox_nodes", ["cluster_id"])
        op.create_index("ix_proxmox_nodes_node_name", "proxmox_nodes", ["node_name"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "proxmox_storages"):
        op.create_table(
            "proxmox_storages",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("node_id", sa.Integer(), sa.ForeignKey("proxmox_nodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("storage_name", sa.String(length=255), nullable=False),
            sa.Column("storage_type", sa.String(length=64), nullable=True),
            sa.Column("total_bytes", sa.Integer(), nullable=True),
            sa.Column("used_bytes", sa.Integer(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index("ix_proxmox_storages_id", "proxmox_storages", ["id"])
        op.create_index("ix_proxmox_storages_node_id", "proxmox_storages", ["node_id"])
        op.create_index("ix_proxmox_storages_storage_name", "proxmox_storages", ["storage_name"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "proxmox_templates"):
        op.create_table(
            "proxmox_templates",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("node_id", sa.Integer(), sa.ForeignKey("proxmox_nodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("vmid", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("storage_name", sa.String(length=255), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index("ix_proxmox_templates_id", "proxmox_templates", ["id"])
        op.create_index("ix_proxmox_templates_node_id", "proxmox_templates", ["node_id"])
        op.create_index("ix_proxmox_templates_vmid", "proxmox_templates", ["vmid"])
        op.create_index("ix_proxmox_templates_name", "proxmox_templates", ["name"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "proxmox_capacity_snapshots"):
        op.create_table(
            "proxmox_capacity_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("node_id", sa.Integer(), sa.ForeignKey("proxmox_nodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("cpu_total", sa.Float(), nullable=True),
            sa.Column("cpu_used", sa.Float(), nullable=True),
            sa.Column("ram_total_bytes", sa.Integer(), nullable=True),
            sa.Column("ram_used_bytes", sa.Integer(), nullable=True),
            sa.Column("storage_total_bytes", sa.Integer(), nullable=True),
            sa.Column("storage_used_bytes", sa.Integer(), nullable=True),
            sa.Column("overcommit_ratio", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index("ix_proxmox_capacity_snapshots_id", "proxmox_capacity_snapshots", ["id"])
        op.create_index("ix_proxmox_capacity_snapshots_node_id", "proxmox_capacity_snapshots", ["node_id"])
        op.create_index("ix_proxmox_capacity_snapshots_created_at", "proxmox_capacity_snapshots", ["created_at"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "ip_subnets"):
        op.create_table(
            "ip_subnets",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("cidr", sa.String(length=64), nullable=False),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="SET NULL"), nullable=True),
            sa.Column("range_start", sa.String(length=64), nullable=True),
            sa.Column("range_end", sa.String(length=64), nullable=True),
            sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("allocation_strategy", sa.String(length=64), nullable=False, server_default="first_free"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("cidr", name="uq_ip_subnets_cidr"),
        )
        op.create_index("ix_ip_subnets_id", "ip_subnets", ["id"])
        op.create_index("ix_ip_subnets_cidr", "ip_subnets", ["cidr"])
        op.create_index("ix_ip_subnets_location_id", "ip_subnets", ["location_id"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "ip_addresses"):
        op.create_table(
            "ip_addresses",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("subnet_id", sa.Integer(), sa.ForeignKey("ip_subnets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("ip_address", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="free"),
            sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("ip_address", name="uq_ip_addresses_ip_address"),
        )
        op.create_index("ix_ip_addresses_id", "ip_addresses", ["id"])
        op.create_index("ix_ip_addresses_subnet_id", "ip_addresses", ["subnet_id"])
        op.create_index("ix_ip_addresses_ip_address", "ip_addresses", ["ip_address"])
        op.create_index("ix_ip_addresses_state", "ip_addresses", ["state"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "service_ip_assignments"):
        op.create_table(
            "service_ip_assignments",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
            sa.Column("ip_id", sa.Integer(), sa.ForeignKey("ip_addresses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("username", sa.String(length=255), nullable=True),
            sa.Column("password", sa.String(length=255), nullable=True),
            sa.Column("assigned_by", sa.String(length=128), nullable=True),
            sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("ip_id", name="uq_service_ip_assignments_ip_id"),
        )
        op.create_index("ix_service_ip_assignments_id", "service_ip_assignments", ["id"])
        op.create_index("ix_service_ip_assignments_service_id", "service_ip_assignments", ["service_id"])
        op.create_index("ix_service_ip_assignments_ip_id", "service_ip_assignments", ["ip_id"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "service_ip_assignment_history"):
        op.create_table(
            "service_ip_assignment_history",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="SET NULL"), nullable=True),
            sa.Column("ip_address", sa.String(length=64), nullable=False),
            sa.Column("subnet_cidr", sa.String(length=64), nullable=True),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("username", sa.String(length=255), nullable=True),
            sa.Column("assigned_by", sa.String(length=128), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index("ix_service_ip_assignment_history_id", "service_ip_assignment_history", ["id"])
        op.create_index("ix_service_ip_assignment_history_service_id", "service_ip_assignment_history", ["service_id"])
        op.create_index("ix_service_ip_assignment_history_ip_address", "service_ip_assignment_history", ["ip_address"])
        op.create_index("ix_service_ip_assignment_history_action", "service_ip_assignment_history", ["action"])
        op.create_index("ix_service_ip_assignment_history_created_at", "service_ip_assignment_history", ["created_at"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_table(inspector, "service_ip_assignment_history"):
        op.drop_table("service_ip_assignment_history")
    if _has_table(inspector, "service_ip_assignments"):
        op.drop_table("service_ip_assignments")
    if _has_table(inspector, "ip_addresses"):
        op.drop_table("ip_addresses")
    if _has_table(inspector, "ip_subnets"):
        op.drop_table("ip_subnets")
    if _has_table(inspector, "proxmox_capacity_snapshots"):
        op.drop_table("proxmox_capacity_snapshots")
    if _has_table(inspector, "proxmox_templates"):
        op.drop_table("proxmox_templates")
    if _has_table(inspector, "proxmox_storages"):
        op.drop_table("proxmox_storages")
    if _has_table(inspector, "proxmox_nodes"):
        op.drop_table("proxmox_nodes")
    if _has_table(inspector, "proxmox_clusters"):
        op.drop_table("proxmox_clusters")
    if _has_table(inspector, "product_family_os_profiles"):
        op.drop_table("product_family_os_profiles")
    if _has_table(inspector, "os_profiles"):
        op.drop_table("os_profiles")
    if _has_table(inspector, "products"):
        op.drop_table("products")
    if _has_table(inspector, "product_families"):
        op.drop_table("product_families")

    inspector = sa.inspect(conn)
    if _has_table(inspector, "services"):
        if _has_index(inspector, "services", "ix_services_os_code"):
            op.drop_index("ix_services_os_code", table_name="services")
        if _has_index(inspector, "services", "ix_services_product_code"):
            op.drop_index("ix_services_product_code", table_name="services")
        if _has_index(inspector, "services", "ix_services_service_type"):
            op.drop_index("ix_services_service_type", table_name="services")

        if _has_column(inspector, "services", "product_snapshot"):
            op.drop_column("services", "product_snapshot")
        if _has_column(inspector, "services", "os_code"):
            op.drop_column("services", "os_code")
        if _has_column(inspector, "services", "product_code"):
            op.drop_column("services", "product_code")
        if _has_column(inspector, "services", "service_type"):
            op.drop_column("services", "service_type")
