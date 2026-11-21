"""Row locks for billing, always taken in one order.

Every transaction that locks more than one billing row takes the locks in the
order of ``LOCK_ORDER`` and never takes an earlier kind after a later one.
Locking a row the transaction already holds again is a no-op and allowed at any
point. Rows that start a gateway transaction (the USDT chain cursor and deposit,
the webhook event claim) are taken before all of these.

An order is locked before its invoice: an order invoice belongs to exactly one
order (``invoices.order_id`` is set when the invoice is created and never
changes), so ``for_invoice`` reads it without a lock and locks the order first.

A service billing row comes first of all: the recurring billing worker starts
from it and then funds and settles the cycle invoice, so ``for_invoice`` locks
the service billing of a cycle invoice before anything else. Every path that
locks a billing cycle holds its service billing first, so the billing row also
serialises work on its cycles.

Every lock re-reads the row (``populate_existing``) after flushing the
session, so a caller always sees the committed state of a locked row together
with its own pending changes.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session

from app.models.commerce_coupon_tax import Coupon
from app.models.commerce_order import Order
from app.models.reseller import (
    BillingCycle,
    Invoice,
    InvoicePurpose,
    Payment,
    Reseller,
    ServiceBilling,
)

LOCK_ORDER = (
    "service_billing",
    "reseller",
    "billing_account",
    "order",
    "invoice",
    "payment",
    "billing_cycle",
    "coupon",
    "refund",
    "number_counter",
)


def _lock(
    db: Session,
    kind: str,
    row_id: Optional[int],
    statement: Select,
    *,
    skip_locked: bool = False,
) -> Any:
    """Run one locking SELECT of ``kind``; every billing lock goes through here.

    ``skip_locked`` skips rows another transaction holds where the database
    supports it (MySQL, PostgreSQL) and is ignored elsewhere.
    """
    db.flush()
    skip = skip_locked and db.get_bind().dialect.name in {"mysql", "postgresql"}
    return db.execute(
        statement.with_for_update(skip_locked=skip).execution_options(
            populate_existing=True
        )
    )


class BillingLocks:
    @staticmethod
    def service_billing(
        db: Session,
        billing_id: int,
        *,
        where: Sequence[Any] = (),
        skip_locked: bool = False,
    ) -> Optional[ServiceBilling]:
        """Lock a service billing row; None if it is gone, no longer matches ``where``, or skipped."""
        return _lock(
            db,
            "service_billing",
            billing_id,
            select(ServiceBilling).where(ServiceBilling.id == billing_id, *where),
            skip_locked=skip_locked,
        ).scalar_one_or_none()

    @staticmethod
    def billing_cycle(
        db: Session, statement: Select, *, skip_locked: bool = False
    ) -> Optional[BillingCycle]:
        """Lock the billing cycle a ``select(BillingCycle)`` statement returns."""
        return _lock(
            db, "billing_cycle", None, statement, skip_locked=skip_locked
        ).scalar_one_or_none()

    @staticmethod
    def reseller(db: Session, reseller_id: int) -> Optional[Reseller]:
        return _lock(
            db, "reseller", reseller_id, select(Reseller).where(Reseller.id == reseller_id)
        ).scalar_one_or_none()

    @staticmethod
    def order(db: Session, order_id: int) -> Optional[Order]:
        return _lock(
            db, "order", order_id, select(Order).where(Order.id == order_id)
        ).scalar_one_or_none()

    @staticmethod
    def invoice_row(db: Session, invoice_id: int) -> Optional[Invoice]:
        """Lock the invoice row alone; callers holding no order lock use ``for_invoice``."""
        if db.get_bind().dialect.name == "sqlite":
            # SQLite ignores SELECT ... FOR UPDATE. A no-op UPDATE acquires its
            # database write lock before a gateway call can race settlement.
            db.execute(
                update(Invoice)
                .where(Invoice.id == invoice_id)
                .values(updated_at=Invoice.updated_at)
            )
        return _lock(
            db, "invoice", invoice_id, select(Invoice).where(Invoice.id == invoice_id)
        ).scalar_one_or_none()

    @staticmethod
    def for_invoice(
        db: Session, invoice_id: int, *, reseller: bool = False
    ) -> Optional[Invoice]:
        """Lock an invoice after the rows that come before it.

        Locks the service billing of a cycle invoice, then (with
        ``reseller=True``, for transactions that go on to write the reseller
        credit ledger) the invoice's reseller, then its order when it has one,
        then the invoice.
        """
        db.flush()
        ids = db.execute(
            select(Invoice.reseller_id, Invoice.order_id, Invoice.purpose).where(
                Invoice.id == invoice_id
            )
        ).first()
        if ids is None:
            return None
        if ids.purpose == InvoicePurpose.CYCLE_CHARGE:
            billing_id = db.scalar(
                select(BillingCycle.service_billing_id).where(
                    BillingCycle.invoice_id == invoice_id
                )
            )
            if billing_id is not None:
                BillingLocks.service_billing(db, int(billing_id))
        if reseller and ids.reseller_id is not None:
            BillingLocks.reseller(db, int(ids.reseller_id))
        if ids.order_id is not None:
            BillingLocks.order(db, int(ids.order_id))
        return BillingLocks.invoice_row(db, invoice_id)

    @staticmethod
    def payments(db: Session, statement: Select) -> list[Payment]:
        """Lock the payments a ``select(Payment)`` statement returns."""
        return list(_lock(db, "payment", None, statement).scalars())

    @staticmethod
    def coupon(db: Session, coupon_id: int) -> Coupon:
        return _lock(
            db, "coupon", coupon_id, select(Coupon).where(Coupon.id == coupon_id)
        ).scalar_one()
