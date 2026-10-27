"""widen installation_tasks.logs to MEDIUMTEXT

Install scripts now upload every log under /tmp on completion, which can exceed
TEXT's 64 KB limit (strict mode rejects the update).

Revision ID: widen_installation_task_logs
Revises: add_boot_task_boot_method
Create Date: 2026-10-27 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "widen_installation_task_logs"
down_revision: Union[str, Sequence[str], None] = "add_boot_task_boot_method"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    op.alter_column(
        "installation_tasks",
        "logs",
        existing_type=sa.Text(),
        type_=mysql.MEDIUMTEXT(),
        existing_nullable=True,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    op.alter_column(
        "installation_tasks",
        "logs",
        existing_type=mysql.MEDIUMTEXT(),
        type_=sa.Text(),
        existing_nullable=True,
    )
