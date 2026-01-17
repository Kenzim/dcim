"""add_iso_boot_and_installation_tasks

Revision ID: add_iso_install_tasks
Revises: add_boot_tasks
Create Date: 2026-01-17 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_iso_install_tasks'
down_revision: Union[str, Sequence[str], None] = 'add_boot_tasks'  # References revision ID from add_boot_tasks_table.py
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Check if columns already exist (in case migration partially ran)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'boot_tasks' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('boot_tasks')}
        
        # Add ISO boot support to boot_tasks table (only if columns don't exist)
        if 'iso_url' not in columns:
            op.add_column('boot_tasks', sa.Column('iso_url', sa.String(length=512), nullable=True))
        if 'description' not in columns:
            op.add_column('boot_tasks', sa.Column('description', sa.Text(), nullable=True))
        
        # Update boottype enum to include 'iso'
        # For MySQL/MariaDB: Use MODIFY COLUMN to update enum
        from sqlalchemy import text
        try:
            op.execute(text("ALTER TABLE boot_tasks MODIFY COLUMN boot_type ENUM('local', 'linux_script', 'iso') NOT NULL"))
        except Exception:
            # Column might already have the correct enum, or might be using a different type
            pass
    else:
        # Table doesn't exist yet, columns will be added when table is created
        pass
    
    # Create installation_tasks table
    op.create_table(
        'installation_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('boot_task_id', sa.Integer(), nullable=False),
        sa.Column('template_id', sa.String(length=255), nullable=True),
        sa.Column('template_parameters', sa.JSON(), nullable=True),
        sa.Column('status', sa.Enum('pending', 'in_progress', 'completed', 'failed', 'cancelled', name='installationstatus'), nullable=False),
        sa.Column('os_name', sa.String(length=255), nullable=True),
        sa.Column('os_version', sa.String(length=255), nullable=True),
        sa.Column('progress_percent', sa.Integer(), nullable=True),
        sa.Column('logs', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ),
        sa.ForeignKeyConstraint(['boot_task_id'], ['boot_tasks.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_installation_tasks_id'), 'installation_tasks', ['id'], unique=False)
    op.create_index(op.f('ix_installation_tasks_server_id'), 'installation_tasks', ['server_id'], unique=False)
    op.create_index(op.f('ix_installation_tasks_boot_task_id'), 'installation_tasks', ['boot_task_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_installation_tasks_boot_task_id'), table_name='installation_tasks')
    op.drop_index(op.f('ix_installation_tasks_server_id'), table_name='installation_tasks')
    op.drop_index(op.f('ix_installation_tasks_id'), table_name='installation_tasks')
    op.drop_table('installation_tasks')
    
    # Drop enum
    sa.Enum(name='installationstatus').drop(op.get_bind(), checkfirst=True)
    
    # Remove columns from boot_tasks
    op.drop_column('boot_tasks', 'description')
    op.drop_column('boot_tasks', 'iso_url')
    
    # Note: Removing enum values is complex and database-specific
    # For PostgreSQL, you'd need to recreate the enum
    # For SQLite, no action needed as it's just strings
