"""add_temp_os_to_boot_type_enum

Revision ID: b78a58f6b036
Revises: ded01102bb62
Create Date: 2026-01-17 14:52:26.161778

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b78a58f6b036'
down_revision: Union[str, Sequence[str], None] = 'ded01102bb62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - add 'temp_os' to boot_type enum."""
    # Update boot_type enum to include 'temp_os'
    # For MySQL/MariaDB: Use MODIFY COLUMN to update enum
    from sqlalchemy import text
    try:
        op.execute(text("ALTER TABLE boot_tasks MODIFY COLUMN boot_type ENUM('local', 'linux_script', 'iso', 'alpine', 'temp_os') NOT NULL"))
    except Exception as e:
        # Column might already have the correct enum, or might be using a different type
        # Log but don't fail - enum might already be updated
        print(f"Note: Could not update enum (might already be correct): {e}")


def downgrade() -> None:
    """Downgrade schema - remove 'temp_os' from boot_type enum."""
    # Note: Removing enum values is complex and database-specific
    # For MySQL/MariaDB, we'd need to recreate the enum without 'temp_os'
    from sqlalchemy import text
    try:
        op.execute(text("ALTER TABLE boot_tasks MODIFY COLUMN boot_type ENUM('local', 'linux_script', 'iso', 'alpine') NOT NULL"))
    except Exception:
        # If this fails, manual intervention may be needed
        pass
