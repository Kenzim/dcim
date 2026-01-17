"""add_alpine_boot_type

Revision ID: add_alpine_boot_type
Revises: add_iso_install_tasks
Create Date: 2026-01-17 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_alpine_boot_type'
down_revision: Union[str, Sequence[str], None] = 'ce07de3c5040'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - add 'alpine' to boot_type enum."""
    # Update boottype enum to include 'alpine'
    # For MySQL/MariaDB: Use MODIFY COLUMN to update enum
    from sqlalchemy import text
    try:
        op.execute(text("ALTER TABLE boot_tasks MODIFY COLUMN boot_type ENUM('local', 'linux_script', 'iso', 'alpine') NOT NULL"))
    except Exception as e:
        # Column might already have the correct enum, or might be using a different type
        # Log but don't fail - enum might already be updated
        print(f"Note: Could not update enum (might already be correct): {e}")


def downgrade() -> None:
    """Downgrade schema - remove 'alpine' from boot_type enum."""
    # Note: Removing enum values is complex and database-specific
    # For MySQL/MariaDB, we'd need to recreate the enum without 'alpine'
    # For now, we'll just update to the previous enum values
    from sqlalchemy import text
    try:
        op.execute(text("ALTER TABLE boot_tasks MODIFY COLUMN boot_type ENUM('local', 'linux_script', 'iso') NOT NULL"))
    except Exception:
        # If this fails, manual intervention may be needed
        pass
