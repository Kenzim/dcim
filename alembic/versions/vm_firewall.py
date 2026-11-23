"""VM firewall: service firewalls, rules, security groups, IP sets, baselines and location settings

Creates the tables of docs/vm-firewall.md section 4. Additive only: no existing table
changes. Foreign keys to existing tables are left out on SQLite, and the check that a rule
has exactly one owner exists only on MariaDB/MySQL (the service layer enforces it everywhere).

Revision ID: vm_firewall
Revises: billing_p2_email_money
Create Date: 2026-11-23 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "vm_firewall"
down_revision: Union[str, Sequence[str], None] = "billing_p2_email_money"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    "firewall_rules",
    "firewall_ipset_entries",
    "firewall_ipsets",
    "firewall_groups",
    "firewall_baselines",
    "firewall_location_settings",
    "service_firewalls",
)

ENUM = sa.String(16)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _external_fk(target: str, ondelete: str) -> list:
    """A foreign key to a table that predates this revision; SQLite keeps the column plain."""
    if op.get_bind().dialect.name == "sqlite":
        return []
    return [sa.ForeignKey(target, ondelete=ondelete)]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    op.create_table(
        "service_firewalls",
        sa.Column("service_id", sa.Integer(), *_external_fk("services.id", "CASCADE"), primary_key=True),
        sa.Column("backend", sa.String(32), nullable=False),
        sa.Column("client_enabled", sa.Boolean(), nullable=False),
        sa.Column("policy_in", ENUM, nullable=False),
        sa.Column("policy_out", ENUM, nullable=False),
        sa.Column("log_level_in", ENUM, nullable=False),
        sa.Column("log_level_out", ENUM, nullable=False),
        sa.Column("managed", sa.Boolean(), nullable=False),
        sa.Column("enforce_baseline", sa.Boolean(), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("admin_overrides", sa.JSON(), nullable=True),
        sa.Column("net0_bridge", sa.String(32), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("applied_revision", sa.Integer(), nullable=True),
        sa.Column("desired_hash", sa.String(64), nullable=True),
        sa.Column("applied_hash", sa.String(64), nullable=True),
        sa.Column("applied_digest", sa.String(64), nullable=True),
        sa.Column("nic_flag", ENUM, nullable=False),
        sa.Column("state", ENUM, nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drift_repairs_24h", sa.Integer(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_service_firewalls_state", "service_firewalls", ["state"])

    op.create_table(
        "firewall_location_settings",
        sa.Column("location_id", sa.Integer(), *_external_fk("locations.id", "CASCADE"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("nic_flag_on_provision", sa.Boolean(), nullable=False),
        sa.Column("enforce_baseline_on_all", sa.Boolean(), nullable=False),
        sa.Column("smtp_block_new_vms", sa.Boolean(), nullable=False),
        sa.Column("anti_spoof_new_vms", sa.Boolean(), nullable=False),
        sa.Column("ipv6_allowed_prefixes", sa.JSON(), nullable=True),
        sa.Column("readiness", sa.JSON(), nullable=True),
        sa.Column("readiness_checked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )

    for table in ("firewall_groups", "firewall_ipsets"):
        extra = [sa.Column("purpose", ENUM, nullable=False)] if table == "firewall_ipsets" else []
        op.create_table(
            table,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(48), nullable=False),
            sa.Column("description", sa.String(255), nullable=True),
            sa.Column("scope", ENUM, nullable=False),
            sa.Column("owner_user_id", sa.Integer(), *_external_fk("users.id", "CASCADE"), nullable=True),
            sa.Column("client_selectable", sa.Boolean(), nullable=False),
            *extra,
            sa.Column("revision", sa.Integer(), nullable=False),
            *_timestamps(),
            sa.UniqueConstraint("owner_user_id", "name", name=f"uq_{table}_owner_name"),
        )
        op.create_index(f"ix_{table}_owner_user_id", table, ["owner_user_id"])

    op.create_table(
        "firewall_ipset_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "ipset_id", sa.Integer(), sa.ForeignKey("firewall_ipsets.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("cidr", sa.String(49), nullable=False),
        sa.Column("nomatch", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.String(120), nullable=True),
        sa.UniqueConstraint("ipset_id", "cidr", name="uq_firewall_ipset_entries_ipset_cidr"),
    )
    op.create_index("ix_firewall_ipset_entries_ipset_id", "firewall_ipset_entries", ["ipset_id"])

    op.create_table(
        "firewall_baselines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("scope_type", ENUM, nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("scope_type", "scope_id", name="uq_firewall_baselines_scope"),
    )

    checks = []
    if dialect in ("mysql", "mariadb"):
        checks.append(
            sa.CheckConstraint(
                "(service_id IS NOT NULL) + (group_id IS NOT NULL) + (baseline_id IS NOT NULL) = 1",
                name="ck_firewall_rules_one_owner",
            )
        )
    op.create_table(
        "firewall_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("service_firewalls.service_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("firewall_groups.id", ondelete="CASCADE"), nullable=True),
        sa.Column(
            "baseline_id", sa.Integer(), sa.ForeignKey("firewall_baselines.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("ref_group_id", sa.Integer(), sa.ForeignKey("firewall_groups.id"), nullable=True),
        sa.Column("direction", ENUM, nullable=True),
        sa.Column("action", ENUM, nullable=True),
        sa.Column("ip_version", ENUM, nullable=False),
        sa.Column("protocol", sa.String(16), nullable=True),
        sa.Column("ports", sa.String(128), nullable=True),
        sa.Column("source_ports", sa.String(128), nullable=True),
        sa.Column("icmp_type", sa.String(32), nullable=True),
        sa.Column("remote_cidrs", sa.JSON(), nullable=True),
        sa.Column("remote_ipset_id", sa.Integer(), sa.ForeignKey("firewall_ipsets.id"), nullable=True),
        sa.Column("local_cidrs", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("log", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.String(120), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), *_external_fk("users.id", "SET NULL"), nullable=True),
        *_timestamps(),
        *checks,
    )
    op.create_index("ix_firewall_rules_service_position", "firewall_rules", ["service_id", "position"])
    op.create_index("ix_firewall_rules_group_position", "firewall_rules", ["group_id", "position"])
    op.create_index("ix_firewall_rules_baseline_position", "firewall_rules", ["baseline_id", "position"])
    op.create_index("ix_firewall_rules_ref_group_id", "firewall_rules", ["ref_group_id"])
    op.create_index("ix_firewall_rules_remote_ipset_id", "firewall_rules", ["remote_ipset_id"])


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
