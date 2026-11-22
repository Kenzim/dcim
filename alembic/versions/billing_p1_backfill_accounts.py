"""Billing phase 1: A3 account backfill

Batched and idempotent (docs/billing-phase1-design.md 2.2, revision 7):

(a) a reseller ``billing_accounts`` row for every reseller without one;
(b) each reseller account's ``credit_balance_cents`` set from
    ``resellers.cached_balance_cents``;
(c) ``credit_ledger_entries.billing_account_id`` set from the reseller's account;
(d) an "Opening balance (A3 migration)" adjustment entry for every retail
    account with a non-zero cache and no ledger entry.

Each batch commits on its own (autocommit block). The same code runs after the
deploy with ``python -m app.services.billing_backfill <name>``. Row updates
only; the downgrade keeps the data (it is valid for the older schema too,
except retail opening entries, which revision ``billing_p1_ledger_accounts``
refuses to downgrade past).

Revision ID: billing_p1_backfill_accounts
Revises: billing_p1_cycles
Create Date: 2026-11-21 13:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "billing_p1_backfill_accounts"
down_revision: Union[str, Sequence[str], None] = "billing_p1_cycles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.services import billing_backfill

    with op.get_context().autocommit_block():
        billing_backfill.backfill_accounts(op.get_bind())


def downgrade() -> None:
    # Backfilled values are facts the older schema can hold; nothing to undo.
    pass
