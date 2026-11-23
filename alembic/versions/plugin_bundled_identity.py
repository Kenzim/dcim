"""Plugin installations record which root they were installed from

Adds ``plugin_installations.bundled`` (not null, default false): true when the
plugin was installed from the bundled root (the plugins Rackflow ships), false
when it came from the plugins folder. The host uses it to refuse an upgrade, a
load or an offered upgrade where the package now found under the installation's
name comes from the other root.

Backfill, decided from where the installation's directory exists now and never
from the stored code hash alone, because a Rackflow release that changes a
bundled plugin's code leaves a genuine bundled installation with an older hash:

* only under the bundled root: ``bundled = true``;
* only under the plugins folder, or under neither: ``bundled = false``;
* under both: the hash breaks the tie. A stored hash equal to the bundled copy's
  gives ``true``; one equal to the plugins folder copy's, or to neither, gives
  ``false``, the safe side, because the reserved-name check already refuses the
  plugins folder copy.

A hash that differs from the package on disk stays "files changed" as before and
never makes an installation third-party. The ADD COLUMN is instant on MariaDB 11.

Revision ID: plugin_bundled_identity
Revises: billing_p2_email_money
Create Date: 2026-11-23 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "plugin_bundled_identity"
down_revision: Union[str, Sequence[str], None] = "billing_p2_email_money"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_bundled(directory: str, stored_hash: str, bundled_root, plugins_root, package_hash) -> bool:
    if bundled_root == plugins_root or not directory or "/" in directory or "\\" in directory or directory.startswith("."):
        return False
    in_bundled = (bundled_root / directory / "plugin.json").is_file()
    if not in_bundled:
        return False
    if not (plugins_root / directory / "plugin.json").is_file():
        return True
    try:
        return package_hash(bundled_root / directory) == stored_hash
    except OSError:
        return False


def backfill(bind) -> None:
    from app.plugin_host.loader import bundled_root, package_hash, plugins_root

    table = sa.table(
        "plugin_installations",
        sa.column("id", sa.String),
        sa.column("directory", sa.String),
        sa.column("code_hash", sa.String),
        sa.column("bundled", sa.Boolean),
    )
    bundled_dir, plugins_dir = bundled_root(), plugins_root()
    rows = bind.execute(sa.select(table.c.id, table.c.directory, table.c.code_hash)).all()
    for plugin_id, directory, stored_hash in rows:
        if _is_bundled(directory, stored_hash, bundled_dir, plugins_dir, package_hash):
            bind.execute(table.update().where(table.c.id == plugin_id).values(bundled=True))


def upgrade() -> None:
    op.add_column(
        "plugin_installations",
        sa.Column("bundled", sa.Boolean(), nullable=False, server_default="0"),
    )
    backfill(op.get_bind())


def downgrade() -> None:
    op.drop_column("plugin_installations", "bundled")
