"""Merge bandwidth samples and cable_runs_agnostic heads

Revision ID: merge_bandwidth_cable
Revises: add_bandwidth_samples, cable_runs_agnostic
Create Date: 2026-02-09

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "merge_bandwidth_cable"
down_revision: Union[str, Sequence[str], None] = ("add_bandwidth_samples", "cable_runs_agnostic")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
