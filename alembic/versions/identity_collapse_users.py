"""Collapse ExternalUser + user_external_identity_links into billing fields on users

Every billing identity (WHMCS client, etc.) previously lived in a separate
``external_users`` row plus a ``user_external_identity_links`` row pointing
at the (always auto-created) linked ``User``. Since every non-admin identity
always gets a linked portal ``User`` anyway, that indirection is pure
overhead: this migration folds it into four nullable columns directly on
``users`` (``billing_integration_id``, ``external_user_id``,
``external_username``, ``external_email``), remaps ``services``/``servers``
ownership onto ``owner_user_id``, and drops the two old tables.

Data migration (upgrade):
1. Add the new nullable columns + timestamps to ``users``.
2. For every ``external_users`` row, resolve (or create, mirroring the old
   ``ensure_portal_user_for_external`` unique-username/email logic) its
   linked ``User`` and stamp the billing identity onto that row.
3. Remap ``services.external_user_id`` (old external_users PK) onto
   ``services.owner_user_id`` wherever the owner isn't already set.
4. Drop ``services.external_user_id`` and ``servers.external_user_id``
   (never read independently of the service's owner) plus both old tables.

Downgrade recreates the old tables from the collapsed ``users`` columns, but
this is best-effort/lossy: the original ``external_users``/link primary keys
and the exact server-level ``external_user_id`` values are gone for good
once the upgrade has run (nothing in the app reads them independently of
``owner_user_id`` any more, so nothing downstream depended on them being
stable).

Revision ID: identity_collapse_users
Revises: service_bare_metal_server_id_nullable
Create Date: 2026-07-28
"""
from __future__ import annotations

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "identity_collapse_users"
down_revision: Union[str, None] = "service_bare_metal_server_id_nullable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_USERNAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return column in [c["name"] for c in inspector.get_columns(table)]


def _fk_names_on_column(conn, table: str, column: str) -> list[str]:
    rows = conn.execute(
        sa.text(
            """
            SELECT CONSTRAINT_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table
              AND COLUMN_NAME = :column
              AND REFERENCED_TABLE_NAME IS NOT NULL
            """
        ),
        {"table": table, "column": column},
    ).fetchall()
    return [r[0] for r in rows]


def _drop_fk_and_column(conn, table: str, column: str) -> None:
    for fk_name in _fk_names_on_column(conn, table, column):
        try:
            conn.execute(sa.text(f"ALTER TABLE {table} DROP FOREIGN KEY {fk_name}"))
        except Exception:
            pass
    try:
        conn.execute(sa.text(f"ALTER TABLE {table} DROP INDEX ix_{table}_{column}"))
    except Exception:
        pass
    conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))


def _unique_username(conn, base: str) -> str:
    base = _USERNAME_SAFE_RE.sub("", base or "").strip(".-_") or "client"
    candidate = base
    suffix = 0
    while conn.execute(sa.text("SELECT 1 FROM users WHERE username = :u"), {"u": candidate}).first():
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


def _unique_email(conn, base_email: str | None, fallback_username: str) -> str:
    if base_email and not conn.execute(sa.text("SELECT 1 FROM users WHERE email = :e"), {"e": base_email}).first():
        return base_email
    suffix = 0
    candidate = f"{fallback_username}@clients.rackflow.local"
    while conn.execute(sa.text("SELECT 1 FROM users WHERE email = :e"), {"e": candidate}).first():
        suffix += 1
        candidate = f"{fallback_username}{suffix}@clients.rackflow.local"
    return candidate


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_column(inspector, "users", "billing_integration_id"):
        op.add_column("users", sa.Column("billing_integration_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_users_billing_integration_id",
            "users",
            "billing_integrations",
            ["billing_integration_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_users_billing_integration_id", "users", ["billing_integration_id"])
    if not _has_column(inspector, "users", "external_user_id"):
        op.add_column("users", sa.Column("external_user_id", sa.String(length=255), nullable=True))
        op.create_index("ix_users_external_user_id", "users", ["external_user_id"])
    if not _has_column(inspector, "users", "external_username"):
        op.add_column("users", sa.Column("external_username", sa.String(length=255), nullable=True))
    if not _has_column(inspector, "users", "external_email"):
        op.add_column("users", sa.Column("external_email", sa.String(length=255), nullable=True))
    if not _has_column(inspector, "users", "created_at"):
        op.add_column(
            "users",
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if not _has_column(inspector, "users", "updated_at"):
        op.add_column(
            "users",
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    inspector = sa.inspect(conn)
    has_external_users = _has_table(inspector, "external_users")
    has_links = _has_table(inspector, "user_external_identity_links")

    # eu_id -> resolved users.id, used below to remap services.owner_user_id
    eu_to_user: dict[int, int] = {}

    if has_external_users:
        external_users = conn.execute(
            sa.text(
                "SELECT id, integration_id, external_user_id, external_username, external_email FROM external_users"
            )
        ).fetchall()

        for eu in external_users:
            eu_id, integration_id, ext_user_id, ext_username, ext_email = eu
            user_id = None
            if has_links:
                link = conn.execute(
                    sa.text("SELECT user_id FROM user_external_identity_links WHERE external_user_id = :eid"),
                    {"eid": eu_id},
                ).first()
                if link:
                    user_id = link[0]

            if user_id is None:
                # Mirror ensure_portal_user_for_external: create a new
                # non-admin, blank-password portal account for this
                # never-linked legacy billing identity.
                base_username = ext_username or f"ext{ext_user_id}"
                username = _unique_username(conn, base_username)
                email = _unique_email(conn, ext_email, username)
                result = conn.execute(
                    sa.text(
                        """
                        INSERT INTO users (username, email, password, is_admin)
                        VALUES (:username, :email, NULL, 0)
                        """
                    ),
                    {"username": username, "email": email},
                )
                user_id = result.lastrowid

            conn.execute(
                sa.text(
                    """
                    UPDATE users
                    SET billing_integration_id = :bi, external_user_id = :eu,
                        external_username = :eun, external_email = :eem
                    WHERE id = :uid
                    """
                ),
                {"bi": integration_id, "eu": ext_user_id, "eun": ext_username, "eem": ext_email, "uid": user_id},
            )
            eu_to_user[eu_id] = user_id

    # Remap services.external_user_id (old external_users PK) onto owner_user_id.
    inspector = sa.inspect(conn)
    if _has_column(inspector, "services", "external_user_id"):
        rows = conn.execute(
            sa.text(
                "SELECT id, external_user_id FROM services WHERE owner_user_id IS NULL AND external_user_id IS NOT NULL"
            )
        ).fetchall()
        for service_id, eu_id in rows:
            user_id = eu_to_user.get(eu_id)
            if user_id is not None:
                conn.execute(
                    sa.text("UPDATE services SET owner_user_id = :uid WHERE id = :sid"),
                    {"uid": user_id, "sid": service_id},
                )
        _drop_fk_and_column(conn, "services", "external_user_id")

    inspector = sa.inspect(conn)
    if _has_column(inspector, "servers", "external_user_id"):
        _drop_fk_and_column(conn, "servers", "external_user_id")

    if has_links:
        op.drop_table("user_external_identity_links")

    inspector = sa.inspect(conn)
    if _has_table(inspector, "external_users"):
        for idx in ("ix_external_user_integration_external_id", "ix_external_users_integration_id", "ix_external_users_id"):
            try:
                op.drop_index(idx, table_name="external_users")
            except Exception:
                pass
        op.drop_table("external_users")

    inspector = sa.inspect(conn)
    uniques = [uc["name"] for uc in inspector.get_unique_constraints("users")]
    if "uq_users_billing_identity" not in uniques:
        op.create_unique_constraint(
            "uq_users_billing_identity", "users", ["billing_integration_id", "external_user_id"]
        )


def downgrade() -> None:
    """Best-effort/lossy: recreates external_users + link rows from the
    collapsed users columns. Original external_users PKs and any
    servers.external_user_id values are not recoverable."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    try:
        op.drop_constraint("uq_users_billing_identity", "users", type_="unique")
    except Exception:
        pass

    if not _has_table(inspector, "external_users"):
        op.create_table(
            "external_users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("integration_id", sa.Integer(), nullable=False),
            sa.Column("external_user_id", sa.String(255), nullable=False),
            sa.Column("external_username", sa.String(255), nullable=True),
            sa.Column("external_email", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["integration_id"], ["billing_integrations.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_external_users_id", "external_users", ["id"])
        op.create_index("ix_external_users_integration_id", "external_users", ["integration_id"])
        op.create_index(
            "ix_external_user_integration_external_id", "external_users", ["integration_id", "external_user_id"], unique=True
        )

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
            "ix_user_external_identity_links_external_user_id", "user_external_identity_links", ["external_user_id"]
        )

    conn = op.get_bind()
    billing_users = conn.execute(
        sa.text(
            """
            SELECT id, billing_integration_id, external_user_id, external_username, external_email
            FROM users WHERE billing_integration_id IS NOT NULL AND external_user_id IS NOT NULL
            """
        )
    ).fetchall()
    for user_id, integration_id, ext_user_id, ext_username, ext_email in billing_users:
        result = conn.execute(
            sa.text(
                """
                INSERT INTO external_users (integration_id, external_user_id, external_username, external_email)
                VALUES (:integration_id, :ext_user_id, :ext_username, :ext_email)
                """
            ),
            {
                "integration_id": integration_id,
                "ext_user_id": ext_user_id,
                "ext_username": ext_username,
                "ext_email": ext_email,
            },
        )
        eu_id = result.lastrowid
        conn.execute(
            sa.text(
                "INSERT INTO user_external_identity_links (user_id, external_user_id) VALUES (:uid, :eid)"
            ),
            {"uid": user_id, "eid": eu_id},
        )

    inspector = sa.inspect(conn)
    if not _has_column(inspector, "services", "external_user_id"):
        op.add_column("services", sa.Column("external_user_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_services_external_user_id", "services", "external_users", ["external_user_id"], ["id"]
        )
        op.create_index("ix_services_external_user_id", "services", ["external_user_id"])

    inspector = sa.inspect(conn)
    if not _has_column(inspector, "servers", "external_user_id"):
        op.add_column("servers", sa.Column("external_user_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_servers_external_user_id", "servers", "external_users", ["external_user_id"], ["id"]
        )
        op.create_index("ix_servers_external_user_id", "servers", ["external_user_id"])

    for col, idx in (
        ("updated_at", None),
        ("created_at", None),
        ("external_email", None),
        ("external_username", None),
        ("external_user_id", "ix_users_external_user_id"),
        ("billing_integration_id", "ix_users_billing_integration_id"),
    ):
        inspector = sa.inspect(conn)
        if _has_column(inspector, "users", col):
            if col == "billing_integration_id":
                _drop_fk_and_column(conn, "users", col)
            else:
                if idx:
                    try:
                        op.drop_index(idx, table_name="users")
                    except Exception:
                        pass
                op.drop_column("users", col)
