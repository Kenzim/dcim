"""add_boot_tasks_table

Revision ID: add_boot_tasks
Revises: 6302d7c4069f
Create Date: 2026-01-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_boot_tasks'
down_revision: Union[str, Sequence[str], None] = '6302d7c4069f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create boot_tasks table
    op.create_table(
        'boot_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('boot_type', sa.Enum('local', 'linux_script', name='boottype'), nullable=False),
        sa.Column('kernel_url', sa.String(length=512), nullable=True),
        sa.Column('initrd_url', sa.String(length=512), nullable=True),
        sa.Column('kernel_params', sa.Text(), nullable=True),
        sa.Column('script_url', sa.String(length=512), nullable=True),
        sa.Column('script_content', sa.Text(), nullable=True),
        sa.Column('status', sa.Enum('pending', 'in_progress', 'completed', 'failed', 'cancelled', name='boottaskstatus'), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_boot_tasks_id'), 'boot_tasks', ['id'], unique=False)
    op.create_index(op.f('ix_boot_tasks_server_id'), 'boot_tasks', ['server_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_boot_tasks_server_id'), table_name='boot_tasks')
    op.drop_index(op.f('ix_boot_tasks_id'), table_name='boot_tasks')
    op.drop_table('boot_tasks')
    # Drop enums
    sa.Enum(name='boottype').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='boottaskstatus').drop(op.get_bind(), checkfirst=True)
