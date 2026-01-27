"""add_switch_ports_and_cable_runs

Revision ID: add_switch_ports_and_cable_runs
Revises: rename_plugins_to_server_plugins
Create Date: 2026-01-23 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_switch_ports_and_cable_runs'
down_revision: Union[str, Sequence[str], None] = 'rename_plugins_to_server_plugins'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add switch_ports and cable_runs tables"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # Create switch_ports table if it doesn't exist
    if 'switch_ports' not in tables:
        op.create_table('switch_ports',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('switch_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('if_index', sa.Integer(), nullable=True),
            sa.Column('speed_mbps', sa.Integer(), nullable=True),
            sa.Column('admin_status', sa.Integer(), nullable=True),
            sa.Column('oper_status', sa.Integer(), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['switch_id'], ['network_switches.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_switch_ports_id'), 'switch_ports', ['id'], unique=False)
        op.create_index(op.f('ix_switch_ports_switch_id'), 'switch_ports', ['switch_id'], unique=False)
    
    # Create cable_runs table if it doesn't exist
    if 'cable_runs' not in tables:
        op.create_table('cable_runs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('switch_port_id', sa.Integer(), nullable=False),
            sa.Column('server_port_id', sa.Integer(), nullable=False),
            sa.Column('cable_type', sa.String(length=50), nullable=True),
            sa.Column('speed_mbps', sa.Integer(), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['switch_port_id'], ['switch_ports.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['server_port_id'], ['network_ports.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('switch_port_id', name='uq_cable_run_switch_port'),
            sa.UniqueConstraint('server_port_id', name='uq_cable_run_server_port')
        )
        op.create_index(op.f('ix_cable_runs_id'), 'cable_runs', ['id'], unique=False)
        op.create_index(op.f('ix_cable_runs_switch_port_id'), 'cable_runs', ['switch_port_id'], unique=False)
        op.create_index(op.f('ix_cable_runs_server_port_id'), 'cable_runs', ['server_port_id'], unique=False)


def downgrade() -> None:
    """Remove switch_ports and cable_runs tables"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'cable_runs' in inspector.get_table_names():
        op.drop_index(op.f('ix_cable_runs_server_port_id'), table_name='cable_runs')
        op.drop_index(op.f('ix_cable_runs_switch_port_id'), table_name='cable_runs')
        op.drop_index(op.f('ix_cable_runs_id'), table_name='cable_runs')
        op.drop_table('cable_runs')
    
    if 'switch_ports' in inspector.get_table_names():
        op.drop_index(op.f('ix_switch_ports_switch_id'), table_name='switch_ports')
        op.drop_index(op.f('ix_switch_ports_id'), table_name='switch_ports')
        op.drop_table('switch_ports')
