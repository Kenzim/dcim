"""Batched, idempotent backfills of billing phase 1 (A3 accounts, payment refunds).

Every function takes a SQLAlchemy ``Connection`` and uses plain SQL only (no
ORM models), so alembic revisions ``billing_p1_backfill_accounts`` and
``billing_p1_backfill_payments`` can call them and later model changes cannot
break them. Each function walks the table in primary-key ranges of
``batch_size`` rows and is safe to run again: rows already done are skipped.

Inside a migration the functions run in an ``autocommit_block`` so each batch
commits on its own. From the command line they commit after every batch::

    python -m app.services.billing_backfill ledger_accounts
    python -m app.services.billing_backfill accounts --batch-size 1000

The deploy runbook re-runs ``accounts`` (steps (a) to (d), all idempotent)
once no process of the previous release remains. While the deploy rolls, old
processes write reseller ledger entries without ``billing_account_id`` and
move ``resellers.cached_balance_cents`` without the account cache, so the
first pass leaves both behind; the second pass links the entries and
re-copies the caches. The credit engine shadow scan reports anything still
unlinked or out of step, and the engine cannot switch to ``v2`` while it does.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

DEFAULT_BATCH_SIZE = 5000
OPENING_BALANCE_DESCRIPTION = "Opening balance (A3 migration)"


@dataclass
class BackfillResult:
    name: str
    batches: int = 0
    rows: int = 0


def _id_ranges(
    conn: Connection, table: str, batch_size: int
) -> Iterator[tuple[int, int]]:
    """Yield ``(after_id, upto_id)`` ranges covering ``batch_size`` rows each."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    last = 0
    offset = int(batch_size) - 1
    while True:
        upper = conn.execute(
            text(f"SELECT id FROM {table} WHERE id > :last ORDER BY id LIMIT 1 OFFSET {offset}"),
            {"last": last},
        ).scalar()
        if upper is None:
            upper = conn.execute(
                text(f"SELECT MAX(id) FROM {table} WHERE id > :last"), {"last": last}
            ).scalar()
            if upper is None:
                return
            yield last, int(upper)
            return
        yield last, int(upper)
        last = int(upper)


def _run_batches(
    conn: Connection,
    result: BackfillResult,
    table: str,
    batch_size: int,
    step: Callable[[int, int], int],
    commit: Optional[Callable[[], None]],
) -> BackfillResult:
    for low, high in _id_ranges(conn, table, batch_size):
        changed = step(low, high)
        result.batches += 1
        result.rows += max(0, int(changed or 0))
        if commit is not None:
            commit()
    return result


def create_reseller_accounts(
    conn: Connection,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    commit: Optional[Callable[[], None]] = None,
) -> BackfillResult:
    """(a) Insert a reseller ``billing_accounts`` row for every reseller without one.

    Mirrors ``BillingAccountDAO.ensure_reseller_account``: type ``reseller``,
    the reseller's user, currency USD, status active, and the reseller cache as
    the opening account cache.
    """

    def step(low: int, high: int) -> int:
        return conn.execute(
            text(
                "INSERT INTO billing_accounts "
                "(account_type, user_id, reseller_id, status, currency, credit_balance_cents, "
                "tax_exempt, created_at, updated_at) "
                "SELECT 'reseller', r.user_id, r.id, 'active', 'USD', "
                "COALESCE(r.cached_balance_cents, 0), :tax_exempt, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "FROM resellers r "
                "WHERE r.id > :low AND r.id <= :high "
                "AND NOT EXISTS (SELECT 1 FROM billing_accounts ba WHERE ba.reseller_id = r.id)"
            ),
            {"low": low, "high": high, "tax_exempt": False},
        ).rowcount

    return _run_batches(
        conn, BackfillResult("reseller_accounts"), "resellers", batch_size, step, commit
    )


def sync_reseller_account_balances(
    conn: Connection,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    commit: Optional[Callable[[], None]] = None,
) -> BackfillResult:
    """(b) Set each reseller account's cache to ``resellers.cached_balance_cents``."""

    def step(low: int, high: int) -> int:
        return conn.execute(
            text(
                "UPDATE billing_accounts SET credit_balance_cents = "
                "(SELECT COALESCE(r.cached_balance_cents, 0) FROM resellers r "
                "WHERE r.id = billing_accounts.reseller_id) "
                "WHERE id > :low AND id <= :high "
                "AND account_type = 'reseller' AND reseller_id IS NOT NULL "
                "AND credit_balance_cents <> (SELECT COALESCE(r2.cached_balance_cents, 0) "
                "FROM resellers r2 WHERE r2.id = billing_accounts.reseller_id)"
            ),
            {"low": low, "high": high},
        ).rowcount

    return _run_batches(
        conn, BackfillResult("account_balances"), "billing_accounts", batch_size, step, commit
    )


def link_ledger_accounts(
    conn: Connection,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    commit: Optional[Callable[[], None]] = None,
) -> BackfillResult:
    """(c) Set ``credit_ledger_entries.billing_account_id`` from the reseller's account."""

    def step(low: int, high: int) -> int:
        return conn.execute(
            text(
                "UPDATE credit_ledger_entries SET billing_account_id = "
                "(SELECT ba.id FROM billing_accounts ba "
                "WHERE ba.reseller_id = credit_ledger_entries.reseller_id) "
                "WHERE id > :low AND id <= :high "
                "AND billing_account_id IS NULL AND reseller_id IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM billing_accounts ba2 "
                "WHERE ba2.reseller_id = credit_ledger_entries.reseller_id)"
            ),
            {"low": low, "high": high},
        ).rowcount

    return _run_batches(
        conn, BackfillResult("ledger_accounts"), "credit_ledger_entries", batch_size, step, commit
    )


def open_retail_balances(
    conn: Connection,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    commit: Optional[Callable[[], None]] = None,
) -> BackfillResult:
    """(d) Give each retail account with a non-zero cache and no entries an opening entry.

    The entry is an ``adjustment`` for the cached amount with idempotency key
    ``a3-opening:<account_id>``, so the ledger chain explains the cache.

    Each entry is written by one ``INSERT ... SELECT`` that reads the cache,
    the "no entries yet" test and the key test in the same statement. On
    InnoDB that read takes a shared lock on the account row, so a concurrent
    balance change either finishes first (and its value is copied) or waits.
    """

    def step(low: int, high: int) -> int:
        candidates = conn.execute(
            text(
                "SELECT ba.id FROM billing_accounts ba "
                "WHERE ba.id > :low AND ba.id <= :high "
                "AND ba.account_type = 'client' AND ba.credit_balance_cents <> 0 "
                "ORDER BY ba.id"
            ),
            {"low": low, "high": high},
        ).scalars().all()
        inserted = 0
        for account_id in candidates:
            key = f"a3-opening:{int(account_id)}"
            inserted += conn.execute(
                text(
                    "INSERT INTO credit_ledger_entries "
                    "(reseller_id, billing_account_id, entry_type, amount_cents, "
                    "balance_after_cents, description, idempotency_key, metadata, created_at) "
                    "SELECT NULL, ba.id, 'adjustment', ba.credit_balance_cents, "
                    "ba.credit_balance_cents, :description, :key, :metadata, CURRENT_TIMESTAMP "
                    "FROM billing_accounts ba "
                    "WHERE ba.id = :account_id AND ba.account_type = 'client' "
                    "AND ba.credit_balance_cents <> 0 "
                    "AND NOT EXISTS (SELECT 1 FROM credit_ledger_entries e "
                    "WHERE e.billing_account_id = ba.id) "
                    "AND NOT EXISTS (SELECT 1 FROM credit_ledger_entries k "
                    "WHERE k.idempotency_key = :key)"
                ),
                {
                    "account_id": int(account_id),
                    "description": OPENING_BALANCE_DESCRIPTION,
                    "key": key,
                    "metadata": json.dumps({"source": "billing_p1_backfill_accounts"}),
                },
            ).rowcount
        return inserted

    return _run_batches(
        conn, BackfillResult("opening_balances"), "billing_accounts", batch_size, step, commit
    )


def backfill_payment_refunds(
    conn: Connection,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    commit: Optional[Callable[[], None]] = None,
) -> BackfillResult:
    """Record ``refunded_cents = amount_cents`` on payments already marked refunded."""

    def step(low: int, high: int) -> int:
        return conn.execute(
            text(
                "UPDATE payments SET refunded_cents = amount_cents "
                "WHERE id > :low AND id <= :high "
                "AND status = 'refunded' AND refunded_cents = 0"
            ),
            {"low": low, "high": high},
        ).rowcount

    return _run_batches(
        conn, BackfillResult("payment_refunds"), "payments", batch_size, step, commit
    )


def backfill_accounts(
    conn: Connection,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    commit: Optional[Callable[[], None]] = None,
) -> list[BackfillResult]:
    """Steps (a) to (d) of revision ``billing_p1_backfill_accounts``, in order."""
    return [
        create_reseller_accounts(conn, batch_size=batch_size, commit=commit),
        sync_reseller_account_balances(conn, batch_size=batch_size, commit=commit),
        link_ledger_accounts(conn, batch_size=batch_size, commit=commit),
        open_retail_balances(conn, batch_size=batch_size, commit=commit),
    ]


BACKFILLS: dict[str, Callable[..., object]] = {
    "reseller_accounts": create_reseller_accounts,
    "account_balances": sync_reseller_account_balances,
    "ledger_accounts": link_ledger_accounts,
    "opening_balances": open_retail_balances,
    "accounts": backfill_accounts,
    "payment_refunds": backfill_payment_refunds,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Re-run a billing phase 1 backfill.")
    parser.add_argument("name", choices=sorted(BACKFILLS))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    from app.core.database import engine

    with engine.connect() as conn:
        outcome = BACKFILLS[args.name](conn, batch_size=args.batch_size, commit=conn.commit)
        conn.commit()
    results = outcome if isinstance(outcome, list) else [outcome]
    for item in results:
        print(f"{item.name}: {item.rows} rows in {item.batches} batches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
