"""Billing phase 1: refunded_cents on payments already marked refunded

``UPDATE payments SET refunded_cents = amount_cents WHERE status = 'refunded'
AND refunded_cents = 0``, in primary-key batches that each commit on their own.
This records a fact the status already states; it computes nothing on invoices
(docs/billing-phase1-design.md 2.2, revision 8).

Revision ID: billing_p1_backfill_payments
Revises: billing_p1_backfill_accounts
Create Date: 2026-11-21 13:10:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "billing_p1_backfill_payments"
down_revision: Union[str, Sequence[str], None] = "billing_p1_backfill_accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.services import billing_backfill

    with op.get_context().autocommit_block():
        billing_backfill.backfill_payment_refunds(op.get_bind())


def downgrade() -> None:
    # The column is dropped by billing_p1_refunds' downgrade; nothing to undo here.
    pass
