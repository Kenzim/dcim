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

from app.models.commerce_account import BillingAccount
from app.models.commerce_coupon_tax import Coupon
from app.models.commerce_order import Order
from app.models.refund import Refund
from app.models.reseller import (
    BillingCycle,
    Invoice,
    InvoicePurpose,
    InvoiceSequence,
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
    def billing_account(db: Session, account_id: int) -> Optional[BillingAccount]:
        """Lock a billing account, whose ``credit_balance_cents`` is the A3 cache."""
        return _lock(
            db,
            "billing_account",
            account_id,
            select(BillingAccount).where(BillingAccount.id == account_id),
        ).scalar_one_or_none()

    @staticmethod
    def reseller_and_account(
        db: Session, reseller_id: int
    ) -> tuple[Optional[Reseller], Optional[BillingAccount]]:
        """Lock a reseller and then its reseller billing account, if it has one."""
        reseller = BillingLocks.reseller(db, reseller_id)
        account_id = db.scalar(
            select(BillingAccount.id).where(BillingAccount.reseller_id == reseller_id)
        )
        account = (
            BillingLocks.billing_account(db, int(account_id)) if account_id is not None else None
        )
        return reseller, account

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
        db: Session,
        invoice_id: int,
        *,
        reseller: bool = False,
        billing_account: bool = False,
    ) -> Optional[Invoice]:
        """Lock an invoice after the rows that come before it.

        Locks the service billing of a cycle invoice, then (with
        ``reseller=True``, for transactions that go on to write the reseller
        credit ledger) the invoice's reseller, then (with
        ``billing_account=True``, for transactions that write any account's
        credit ledger) the invoice's billing account, falling back to the
        reseller's account for reseller invoices without one, then its order
        when it has one, then the invoice.
        """
        db.flush()
        ids = db.execute(
            select(
                Invoice.reseller_id,
                Invoice.order_id,
                Invoice.purpose,
                Invoice.billing_account_id,
            ).where(Invoice.id == invoice_id)
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
        if billing_account:
            account_id = ids.billing_account_id
            if account_id is None and ids.reseller_id is not None:
                account_id = db.scalar(
                    select(BillingAccount.id).where(
                        BillingAccount.reseller_id == ids.reseller_id
                    )
                )
            if account_id is not None:
                BillingLocks.billing_account(db, int(account_id))
        if ids.order_id is not None:
            BillingLocks.order(db, int(ids.order_id))
        return BillingLocks.invoice_row(db, invoice_id)

    @staticmethod
    def payment(db: Session, payment_id: int) -> Optional[Payment]:
        """Lock one payment row alone; callers that also need its invoice use ``for_payment``."""
        return _lock(
            db, "payment", payment_id, select(Payment).where(Payment.id == payment_id)
        ).scalar_one_or_none()

    @staticmethod
    def for_payment(
        db: Session,
        payment_id: int,
        *,
        reseller: bool = False,
        billing_account: bool = False,
    ) -> Optional[Payment]:
        """Lock a payment after its invoice and the rows ``for_invoice`` takes first."""
        db.flush()
        invoice_id = db.scalar(select(Payment.invoice_id).where(Payment.id == payment_id))
        if invoice_id is None:
            return None
        BillingLocks.for_invoice(
            db, int(invoice_id), reseller=reseller, billing_account=billing_account
        )
        return BillingLocks.payment(db, payment_id)

    @staticmethod
    def refund(db: Session, refund_id: int) -> Optional[Refund]:
        """Lock one refund row; callers hold its payment chain first (``for_payment``)."""
        return _lock(
            db, "refund", refund_id, select(Refund).where(Refund.id == refund_id)
        ).scalar_one_or_none()

    @staticmethod
    def number_counter(db: Session, name: str) -> Optional[InvoiceSequence]:
        """Lock a numbering counter row inside the caller's transaction (gapless series).

        This is the last lock of any billing transaction.
        """
        return _lock(
            db,
            "number_counter",
            None,
            select(InvoiceSequence).where(InvoiceSequence.name == name),
        ).scalar_one_or_none()

    @staticmethod
    def payments(db: Session, statement: Select) -> list[Payment]:
        """Lock the payments a ``select(Payment)`` statement returns."""
        return list(_lock(db, "payment", None, statement).scalars())

    @staticmethod
    def coupon(db: Session, coupon_id: int) -> Coupon:
        return _lock(
            db, "coupon", coupon_id, select(Coupon).where(Coupon.id == coupon_id)
        ).scalar_one()
