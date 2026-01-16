"""fix_disk_type_enum_values

Revision ID: fix_disk_enum
Revises: 6302d7c4069f
Create Date: 2026-01-16 22:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'fix_disk_enum'
down_revision: Union[str, Sequence[str], None] = '6302d7c4069f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - convert lowercase enum values to uppercase"""
    # Check if disks table exists
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'disks' not in inspector.get_table_names():
        return  # Table doesn't exist yet
    
    columns = {col['name']: col for col in inspector.get_columns('disks')}
    
    # If type column doesn't exist but type_temp does, restore from type_temp
    if 'type' not in columns and 'type_temp' in columns:
        # Copy data from type_temp, convert to uppercase, then recreate type column
        op.execute(text("UPDATE disks SET type_temp = UPPER(type_temp) WHERE type_temp IS NOT NULL"))
        op.add_column('disks', sa.Column('type', sa.Enum('SSD', 'HDD', name='disktype'), nullable=False, server_default='SSD'))
        op.execute(text("UPDATE disks SET type = UPPER(type_temp) WHERE type_temp IS NOT NULL"))
        op.drop_column('disks', 'type_temp')
        return
    
    # If type column exists, update values and modify enum
    if 'type' in columns:
        # Check current enum values - if it's VARCHAR or has lowercase, convert
        col_type = str(columns['type']['type'])
        if 'VARCHAR' in col_type or 'varchar' in col_type.lower():
            # Convert to uppercase and recreate as enum
            op.execute(text("UPDATE disks SET type = UPPER(type)"))
            op.execute(text("ALTER TABLE disks MODIFY COLUMN type ENUM('SSD', 'HDD') NOT NULL"))
        else:
            # Just update any lowercase values
            op.execute(text("UPDATE disks SET type = 'SSD' WHERE type = 'ssd'"))
            op.execute(text("UPDATE disks SET type = 'HDD' WHERE type = 'hdd'"))
            try:
                op.execute(text("ALTER TABLE disks MODIFY COLUMN type ENUM('SSD', 'HDD') NOT NULL"))
            except Exception:
                pass  # Might already be correct


def downgrade() -> None:
    """Downgrade schema - convert uppercase enum values to lowercase"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'disks' not in inspector.get_table_names():
        return
    
    columns = {col['name']: col for col in inspector.get_columns('disks')}
    if 'type' not in columns:
        return
    
    # Update existing uppercase values to lowercase
    op.execute(text("UPDATE disks SET type = 'ssd' WHERE type = 'SSD'"))
    op.execute(text("UPDATE disks SET type = 'hdd' WHERE type = 'HDD'"))
    
    # Alter enum back to lowercase
    try:
        op.execute(text("ALTER TABLE disks MODIFY COLUMN type ENUM('ssd', 'hdd') NOT NULL"))
    except Exception:
        pass
