"""Transactional operations for the account credit ledger.

Every billing account (reseller or retail) has one ledger chain: its entries
ordered by id (docs/billing-phase1-design.md 6.1). The reseller methods
(``credit``, ``debit``, ``adjust``, ``reverse``) keep their signatures and
resolve the reseller's account; the ``*_account`` methods take a billing
account id. The credit engine mode decides which cache is authoritative:

- ``legacy``: the reseller cache only, as before A3; reseller entries are not
  linked to an account. Retail accounts (one cache) chain in every mode; their
  credit features are gated on ``v2`` where they start.
- ``shadow``: the reseller cache is authoritative for reseller accounts and the
  account cache is written beside it as a mirror; differences are recorded as
  check ``c1_cache_pair``.
- ``v2``: the account cache is authoritative and the reseller cache is the
  mirror.

Every public method flushes its changes but deliberately leaves commit and
rollback ownership with the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Union

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dao.reseller_dao import ResellerDAO
from app.models.commerce_account import BillingAccount, BillingAccountType
from app.models.reseller import (
    CreditLedgerEntry,
    CreditLedgerEntryType,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
)

logger = logging.getLogger(__name__)

CREDIT_PAYMENT_GATEWAY = "credit"

# Temporary fallback for ``InvoiceService.UNPAID_STATUSES`` (design 4.2) until
# the invoice engine worker adds it. ``partially_paid`` and ``collections`` are
# harmless here in legacy and shadow only because nothing writes them before
# the invoice engine is v2, so the set gives today's unpaid results.
UNPAID_STATUSES_FALLBACK: tuple[InvoiceStatus, ...] = (
    InvoiceStatus.OPEN,
    InvoiceStatus.PENDING_ACTION,
    InvoiceStatus.OVERDUE,
    InvoiceStatus.PARTIALLY_PAID,
    InvoiceStatus.COLLECTIONS,
)


def unpaid_invoice_statuses() -> tuple[InvoiceStatus, ...]:
    """Invoice statuses that still owe money (design 4.2)."""
    from app.services.invoice_service import InvoiceService

    return tuple(getattr(InvoiceService, "UNPAID_STATUSES", UNPAID_STATUSES_FALLBACK))


def statuses_for_status_filter(requested: InvoiceStatus) -> tuple[InvoiceStatus, ...]:
    """Internal statuses a ``status`` filter on a frozen API matches.

    A filter by one of the public values matches every internal status shown
    as that value (``open`` also matches ``partially_paid``); any other value
    matches itself only.
    """
    from app.services.invoice_service import PUBLIC_INVOICE_STATUSES, public_invoice_status

    if requested.value not in PUBLIC_INVOICE_STATUSES:
        return (requested,)
    return tuple(
        status
        for status in InvoiceStatus
        if status == requested or public_invoice_status(status) == requested.value
    )


class CreditLedgerError(ValueError):
    """Base exception for rejected credit ledger operations."""


class ResellerNotFoundError(CreditLedgerError):
    pass


class BillingAccountNotFoundError(CreditLedgerError):
    pass


class RetailCreditUnavailableError(CreditLedgerError):
    """Retail credit is refused until the credit engine is ``v2`` (design 11.5)."""


class InsufficientCreditError(CreditLedgerError):
    def __init__(
        self,
        *,
        reseller_id: Optional[int],
        available_cents: int,
        requested_cents: int,
        billing_account_id: Optional[int] = None,
    ) -> None:
        self.reseller_id = reseller_id
        self.billing_account_id = billing_account_id
        self.available_cents = available_cents
        self.requested_cents = requested_cents
        owner = (
            f"Reseller {reseller_id}"
            if reseller_id is not None
            else f"Billing account {billing_account_id}"
        )
        super().__init__(
            f"{owner} has {available_cents} cents available; "
            f"{requested_cents} cents required"
        )


class LedgerEntryAlreadyReversedError(CreditLedgerError):
    pass


@dataclass(frozen=True)
class _LedgerEntryContext:
    description: Optional[str] = None
    invoice_id: Optional[int] = None
    payment_id: Optional[int] = None
    service_id: Optional[int] = None
    reversal_of_id: Optional[int] = None
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    metadata: Optional[Mapping[str, Any]] = None
    created_by_user_id: Optional[int] = None


def credit_engine_mode(db: Session) -> str:
    """The effective credit engine mode: ``legacy``, ``shadow`` or ``v2``."""
    from app.services.billing_shadow import billing_engine_mode

    return billing_engine_mode(db, "credit")


def _is_reseller_account(account: BillingAccount) -> bool:
    return (
        account.account_type == BillingAccountType.RESELLER
        and account.reseller_id is not None
    )


class CreditLedgerService:
    @staticmethod
    def _require_integer_cents(amount_cents: int, *, positive: bool) -> None:
        if type(amount_cents) is not int:
            raise CreditLedgerError("amount_cents must be an integer")
        if positive and amount_cents <= 0:
            raise CreditLedgerError("amount_cents must be greater than zero")
        if not positive and amount_cents == 0:
            raise CreditLedgerError("amount_cents must not be zero")

    @staticmethod
    def _lock_reseller(db: Session, reseller_id: int) -> Reseller:
        reseller = ResellerDAO.get_for_update(db, reseller_id)
        if reseller is None:
            raise ResellerNotFoundError(f"Reseller {reseller_id} was not found")
        return reseller

    @staticmethod
    def _lock_reseller_and_account(
        db: Session, reseller_id: int
    ) -> tuple[Reseller, BillingAccount]:
        """Lock the reseller, then its reseller billing account (created if missing)."""
        from app.dao.commerce_dao import BillingAccountDAO
        from app.services.billing_locks import BillingLocks

        reseller = BillingLocks.reseller(db, reseller_id)
        if reseller is None:
            raise ResellerNotFoundError(f"Reseller {reseller_id} was not found")
        account_id = db.scalar(
            select(BillingAccount.id).where(
                BillingAccount.reseller_id == reseller_id,
                BillingAccount.account_type == BillingAccountType.RESELLER,
            )
        )
        if account_id is None:
            return reseller, BillingAccountDAO.ensure_reseller_account(db, reseller)
        account = BillingLocks.billing_account(db, int(account_id))
        return reseller, account

    @staticmethod
    def _append_locked(
        db: Session,
        *,
        reseller: Reseller,
        entry_type: CreditLedgerEntryType,
        signed_amount_cents: int,
        context: _LedgerEntryContext,
        allow_negative_balance: bool = False,
    ) -> CreditLedgerEntry:
        """The legacy write: reseller cache only, entry not linked to an account."""
        if context.idempotency_key is not None:
            existing = db.execute(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.idempotency_key
                    == context.idempotency_key
                )
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.reseller_id != reseller.id
                    or existing.entry_type != entry_type
                    or existing.amount_cents != signed_amount_cents
                    or existing.reversal_of_id != context.reversal_of_id
                ):
                    raise CreditLedgerError(
                        "idempotency_key is already used by a different "
                        "ledger operation"
                    )
                return existing

        new_balance = reseller.cached_balance_cents + signed_amount_cents
        if new_balance < 0 and not allow_negative_balance:
            raise InsufficientCreditError(
                reseller_id=reseller.id,
                available_cents=reseller.cached_balance_cents,
                requested_cents=-signed_amount_cents,
            )

        reseller.cached_balance_cents = new_balance
        entry = CreditLedgerEntry(
            reseller=reseller,
            entry_type=entry_type,
            amount_cents=signed_amount_cents,
            balance_after_cents=new_balance,
            **CreditLedgerService._entry_fields(context),
        )
        db.add(entry)
        db.flush()
        return entry

    @staticmethod
    def _entry_fields(context: _LedgerEntryContext) -> dict[str, Any]:
        return {
            "description": context.description,
            "invoice_id": context.invoice_id,
            "payment_id": context.payment_id,
            "service_id": context.service_id,
            "reversal_of_id": context.reversal_of_id,
            "reference_type": context.reference_type,
            "reference_id": context.reference_id,
            "idempotency_key": context.idempotency_key,
            "ledger_metadata": dict(context.metadata or {}),
            "created_by_user_id": context.created_by_user_id,
        }

    @staticmethod
    def _append(
        db: Session,
        *,
        mode: str,
        account: BillingAccount,
        reseller: Optional[Reseller],
        entry_type: CreditLedgerEntryType,
        signed_amount_cents: int,
        context: _LedgerEntryContext,
        allow_negative_balance: bool = False,
    ) -> CreditLedgerEntry:
        """Write one entry to ``account``'s chain in mode ``shadow`` or ``v2`` (design 6.2).

        The caller holds the reseller lock (reseller accounts) and then the
        account lock.
        """
        if context.idempotency_key is not None:
            existing = db.execute(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.idempotency_key == context.idempotency_key
                )
            ).scalar_one_or_none()
            if existing is not None:
                reseller_id = reseller.id if reseller is not None else None
                same_owner = existing.reseller_id == reseller_id and (
                    existing.billing_account_id == account.id
                    # An entry written before the account link (legacy mode or
                    # an older process) by the same reseller.
                    or (existing.billing_account_id is None and reseller_id is not None)
                )
                if (
                    not same_owner
                    or existing.entry_type != entry_type
                    or existing.amount_cents != signed_amount_cents
                    or existing.reversal_of_id != context.reversal_of_id
                ):
                    raise CreditLedgerError(
                        "idempotency_key is already used by a different "
                        "ledger operation"
                    )
                return existing

        account_before = int(account.credit_balance_cents or 0)
        account_after = account_before + signed_amount_cents
        mirror_after: Optional[int] = None
        if reseller is None:
            before, authoritative = account_before, account_after
        else:
            reseller_before = int(reseller.cached_balance_cents or 0)
            reseller_after = reseller_before + signed_amount_cents
            if mode == "v2":
                before, authoritative, mirror_after = account_before, account_after, reseller_after
            else:
                before, authoritative, mirror_after = reseller_before, reseller_after, account_after
        if authoritative < 0 and not allow_negative_balance:
            raise InsufficientCreditError(
                reseller_id=reseller.id if reseller is not None else None,
                billing_account_id=account.id,
                available_cents=before,
                requested_cents=-signed_amount_cents,
            )

        account.credit_balance_cents = account_after
        if reseller is not None:
            reseller.cached_balance_cents = reseller_after
        entry = CreditLedgerEntry(
            reseller=reseller,
            billing_account_id=account.id,
            entry_type=entry_type,
            amount_cents=signed_amount_cents,
            balance_after_cents=authoritative,
            **CreditLedgerService._entry_fields(context),
        )
        db.add(entry)
        db.flush()

        if mirror_after is not None and mirror_after != authoritative:
            from app.services.billing_shadow import BillingShadowService

            # The mirror keeps its own computed value so the drift stays visible.
            BillingShadowService.record(
                "credit",
                "c1_cache_pair",
                "billing_account",
                account.id,
                reseller.cached_balance_cents,
                account.credit_balance_cents,
                f"ledger_entry:{entry.id}",
            )
        if signed_amount_cents > 0:
            from app.services.billing import settlement

            settlement.fire(
                db,
                settlement.CREDIT_ADDED,
                settlement.CreditAdded(
                    entry=entry,
                    balance_cents=authoritative,
                    currency=account.currency,
                ),
            )
        return entry

    @staticmethod
    def _write_reseller(
        db: Session,
        reseller_id: int,
        *,
        entry_type: CreditLedgerEntryType,
        signed_amount_cents: int,
        context: _LedgerEntryContext,
        allow_negative_balance: bool = False,
    ) -> CreditLedgerEntry:
        mode = credit_engine_mode(db)
        if mode == "legacy":
            reseller = CreditLedgerService._lock_reseller(db, reseller_id)
            return CreditLedgerService._append_locked(
                db,
                reseller=reseller,
                entry_type=entry_type,
                signed_amount_cents=signed_amount_cents,
                context=context,
                allow_negative_balance=allow_negative_balance,
            )
        reseller, account = CreditLedgerService._lock_reseller_and_account(db, reseller_id)
        return CreditLedgerService._append(
            db,
            mode=mode,
            account=account,
            reseller=reseller,
            entry_type=entry_type,
            signed_amount_cents=signed_amount_cents,
            context=context,
            allow_negative_balance=allow_negative_balance,
        )

    @staticmethod
    def _write_account(
        db: Session,
        billing_account_id: int,
        *,
        entry_type: CreditLedgerEntryType,
        signed_amount_cents: int,
        context: _LedgerEntryContext,
        allow_negative_balance: bool = False,
    ) -> CreditLedgerEntry:
        from app.services.billing_locks import BillingLocks

        owner = db.execute(
            select(BillingAccount.account_type, BillingAccount.reseller_id).where(
                BillingAccount.id == billing_account_id
            )
        ).first()
        if owner is None:
            raise BillingAccountNotFoundError(
                f"Billing account {billing_account_id} was not found"
            )
        if owner.account_type == BillingAccountType.RESELLER and owner.reseller_id is not None:
            return CreditLedgerService._write_reseller(
                db,
                int(owner.reseller_id),
                entry_type=entry_type,
                signed_amount_cents=signed_amount_cents,
                context=context,
                allow_negative_balance=allow_negative_balance,
            )
        mode = credit_engine_mode(db)
        account = BillingLocks.billing_account(db, billing_account_id)
        # A retail account has one cache, so its chain is written in every mode;
        # retail credit features are gated on v2 where they start (top-up and
        # apply), so a gateway-confirmed top-up still settles after a rollback.
        return CreditLedgerService._append(
            db,
            mode=mode,
            account=account,
            reseller=None,
            entry_type=entry_type,
            signed_amount_cents=signed_amount_cents,
            context=context,
            allow_negative_balance=allow_negative_balance,
        )

    @staticmethod
    def credit(
        db: Session,
        reseller_id: int,
        amount_cents: int,
        *,
        description: Optional[str] = None,
        invoice_id: Optional[int] = None,
        payment_id: Optional[int] = None,
        service_id: Optional[int] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_by_user_id: Optional[int] = None,
    ) -> CreditLedgerEntry:
        CreditLedgerService._require_integer_cents(amount_cents, positive=True)
        return CreditLedgerService._write_reseller(
            db,
            reseller_id,
            entry_type=CreditLedgerEntryType.TOPUP,
            signed_amount_cents=amount_cents,
            context=_LedgerEntryContext(
                description=description,
                invoice_id=invoice_id,
                payment_id=payment_id,
                service_id=service_id,
                reference_type=reference_type,
                reference_id=reference_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
                created_by_user_id=created_by_user_id,
            ),
        )

    @staticmethod
    def debit(
        db: Session,
        reseller_id: int,
        amount_cents: int,
        *,
        description: Optional[str] = None,
        invoice_id: Optional[int] = None,
        payment_id: Optional[int] = None,
        service_id: Optional[int] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_by_user_id: Optional[int] = None,
        allow_negative_balance: bool = False,
    ) -> CreditLedgerEntry:
        CreditLedgerService._require_integer_cents(amount_cents, positive=True)
        return CreditLedgerService._write_reseller(
            db,
            reseller_id,
            entry_type=CreditLedgerEntryType.CHARGE,
            signed_amount_cents=-amount_cents,
            context=_LedgerEntryContext(
                description=description,
                invoice_id=invoice_id,
                payment_id=payment_id,
                service_id=service_id,
                reference_type=reference_type,
                reference_id=reference_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
                created_by_user_id=created_by_user_id,
            ),
            allow_negative_balance=allow_negative_balance,
        )

    @staticmethod
    def adjust(
        db: Session,
        reseller_id: int,
        amount_cents: int,
        *,
        description: Optional[str] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_by_user_id: Optional[int] = None,
    ) -> CreditLedgerEntry:
        """Apply a signed administrative adjustment without allowing debt."""
        CreditLedgerService._require_integer_cents(amount_cents, positive=False)
        return CreditLedgerService._write_reseller(
            db,
            reseller_id,
            entry_type=CreditLedgerEntryType.ADJUSTMENT,
            signed_amount_cents=amount_cents,
            context=_LedgerEntryContext(
                description=description,
                reference_type=reference_type,
                reference_id=reference_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
                created_by_user_id=created_by_user_id,
            ),
        )

    @staticmethod
    def reverse(
        db: Session,
        reseller_id: int,
        entry_id: int,
        *,
        description: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_by_user_id: Optional[int] = None,
    ) -> CreditLedgerEntry:
        """Append the exact opposite of an existing, unreversed entry."""
        mode = credit_engine_mode(db)
        account: Optional[BillingAccount] = None
        if mode == "legacy":
            reseller = CreditLedgerService._lock_reseller(db, reseller_id)
        else:
            reseller, account = CreditLedgerService._lock_reseller_and_account(
                db, reseller_id
            )
        original = db.execute(
            select(CreditLedgerEntry)
            .where(
                CreditLedgerEntry.id == entry_id,
                CreditLedgerEntry.reseller_id == reseller_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if original is None:
            raise CreditLedgerError(
                f"Ledger entry {entry_id} was not found for reseller {reseller_id}"
            )
        if original.entry_type == CreditLedgerEntryType.REVERSAL:
            raise CreditLedgerError("A reversal entry cannot itself be reversed")

        existing_reversal = db.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.reversal_of_id == original.id
            )
        ).scalar_one_or_none()
        if existing_reversal is not None:
            if (
                idempotency_key is not None
                and existing_reversal.idempotency_key == idempotency_key
            ):
                return existing_reversal
            raise LedgerEntryAlreadyReversedError(
                f"Ledger entry {entry_id} has already been reversed"
            )

        context = _LedgerEntryContext(
            description=description or f"Reversal of ledger entry {original.id}",
            invoice_id=original.invoice_id,
            payment_id=original.payment_id,
            service_id=original.service_id,
            reversal_of_id=original.id,
            reference_type=original.reference_type,
            reference_id=original.reference_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
            created_by_user_id=created_by_user_id,
        )
        # Gateway chargebacks and top-up reversals can legitimately put the
        # account into debt. Deploy/cycle debits still reject an insufficient
        # balance; a negative balance blocks future spends until a later
        # credit restores it.
        if account is None:
            return CreditLedgerService._append_locked(
                db,
                reseller=reseller,
                entry_type=CreditLedgerEntryType.REVERSAL,
                signed_amount_cents=-original.amount_cents,
                allow_negative_balance=True,
                context=context,
            )
        return CreditLedgerService._append(
            db,
            mode=mode,
            account=account,
            reseller=reseller,
            entry_type=CreditLedgerEntryType.REVERSAL,
            signed_amount_cents=-original.amount_cents,
            allow_negative_balance=True,
            context=context,
        )

    # Billing account ledger (A3, docs/billing-phase1-design.md 6.1 and 6.2).

    @staticmethod
    def credit_account(
        db: Session,
        billing_account_id: int,
        amount_cents: int,
        *,
        description: Optional[str] = None,
        invoice_id: Optional[int] = None,
        payment_id: Optional[int] = None,
        service_id: Optional[int] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_by_user_id: Optional[int] = None,
    ) -> CreditLedgerEntry:
        """Credit a billing account (retail or reseller); keywords as ``credit``."""
        CreditLedgerService._require_integer_cents(amount_cents, positive=True)
        return CreditLedgerService._write_account(
            db,
            billing_account_id,
            entry_type=CreditLedgerEntryType.TOPUP,
            signed_amount_cents=amount_cents,
            context=_LedgerEntryContext(
                description=description,
                invoice_id=invoice_id,
                payment_id=payment_id,
                service_id=service_id,
                reference_type=reference_type,
                reference_id=reference_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
                created_by_user_id=created_by_user_id,
            ),
        )

    @staticmethod
    def debit_account(
        db: Session,
        billing_account_id: int,
        amount_cents: int,
        *,
        description: Optional[str] = None,
        invoice_id: Optional[int] = None,
        payment_id: Optional[int] = None,
        service_id: Optional[int] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_by_user_id: Optional[int] = None,
        allow_negative_balance: bool = False,
    ) -> CreditLedgerEntry:
        """Debit a billing account; keywords as ``debit``."""
        CreditLedgerService._require_integer_cents(amount_cents, positive=True)
        return CreditLedgerService._write_account(
            db,
            billing_account_id,
            entry_type=CreditLedgerEntryType.CHARGE,
            signed_amount_cents=-amount_cents,
            context=_LedgerEntryContext(
                description=description,
                invoice_id=invoice_id,
                payment_id=payment_id,
                service_id=service_id,
                reference_type=reference_type,
                reference_id=reference_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
                created_by_user_id=created_by_user_id,
            ),
            allow_negative_balance=allow_negative_balance,
        )

    @staticmethod
    def adjust_account(
        db: Session,
        billing_account_id: int,
        amount_cents: int,
        *,
        description: Optional[str] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_by_user_id: Optional[int] = None,
    ) -> CreditLedgerEntry:
        """Signed adjustment of a billing account without allowing debt; keywords as ``adjust``."""
        CreditLedgerService._require_integer_cents(amount_cents, positive=False)
        return CreditLedgerService._write_account(
            db,
            billing_account_id,
            entry_type=CreditLedgerEntryType.ADJUSTMENT,
            signed_amount_cents=amount_cents,
            context=_LedgerEntryContext(
                description=description,
                reference_type=reference_type,
                reference_id=reference_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
                created_by_user_id=created_by_user_id,
            ),
        )

    @staticmethod
    def balance_cents(db: Session, account: Union[BillingAccount, Reseller]) -> int:
        """The authoritative cached balance (may be negative), by the credit engine mode.

        ``account`` is a billing account or a reseller. Reseller accounts read
        the reseller cache in ``legacy`` and ``shadow`` and the account cache in
        ``v2``; retail accounts always read the account cache.
        """
        mode = credit_engine_mode(db)
        if isinstance(account, Reseller):
            if mode != "v2":
                return int(account.cached_balance_cents or 0)
            linked = db.scalar(
                select(BillingAccount).where(
                    BillingAccount.reseller_id == account.id,
                    BillingAccount.account_type == BillingAccountType.RESELLER,
                )
            )
            if linked is None:
                return int(account.cached_balance_cents or 0)
            return int(linked.credit_balance_cents or 0)
        if _is_reseller_account(account) and mode != "v2":
            reseller = db.get(Reseller, account.reseller_id)
            if reseller is not None:
                return int(reseller.cached_balance_cents or 0)
        return int(account.credit_balance_cents or 0)

    @staticmethod
    def available_cents(db: Session, account: Union[BillingAccount, Reseller]) -> int:
        """Credit the account can spend now: ``max(0, authoritative cache)`` (design 6.2)."""
        return max(0, CreditLedgerService.balance_cents(db, account))

    @staticmethod
    def retail_credit_available(db: Session) -> bool:
        """Retail credit features (top-up, apply, remove) are live (design 11.5 step 3)."""
        from app.services.settings_store import get_setting

        return credit_engine_mode(db) == "v2" and bool(
            get_setting(db, "billing_credit_enabled_retail")
        )

    @staticmethod
    def _remaining_cents(db: Session, invoice: Invoice) -> int:
        from app.services.billing_shadow import billing_engine_mode
        from app.services.invoice_service import InvoiceService

        if billing_engine_mode(db, "invoice") == "v2":
            return int(InvoiceService.balance(db, invoice).remaining_cents)
        return max(
            0, int(invoice.amount_cents) - InvoiceService.allocated_cents(db, invoice.id)
        )

    @staticmethod
    def allocate_to_locked_invoice(
        db: Session,
        invoice: Invoice,
        *,
        reseller: Optional[Reseller],
        amount_cents: int,
        created_by_user_id: Optional[int],
        idempotency_key: Optional[str] = None,
        fire_event: bool = True,
    ) -> Optional[Payment]:
        """Pay ``amount_cents`` of a locked invoice from its owner's credit.

        The caller holds the reseller (reseller invoices), account and invoice
        locks and has checked the amount against the available credit. Writes
        a succeeded ``credit`` payment and the matching debit, then settles the
        invoice. Without ``idempotency_key`` the debit's key is
        ``invoice-credit:<invoice>:<payment>``, the reseller funding path's key.
        """
        from app.services.invoice_service import InvoiceService

        if amount_cents <= 0:
            return None
        ordinal = int(
            db.scalar(
                select(func.count(Payment.id)).where(
                    Payment.invoice_id == invoice.id,
                    Payment.gateway == CREDIT_PAYMENT_GATEWAY,
                )
            )
            or 0
        )
        source = "reseller_credit" if reseller is not None else "account_credit"
        payment = Payment(
            invoice_id=invoice.id,
            gateway=CREDIT_PAYMENT_GATEWAY,
            status=PaymentStatus.SUCCEEDED,
            amount_cents=amount_cents,
            currency=invoice.currency,
            external_ref=f"credit:{invoice.id}:{ordinal + 1}",
            payment_metadata={"source": source},
            processed_at=datetime.now(timezone.utc),
        )
        db.add(payment)
        db.flush()
        keywords = dict(
            description=f"Credit allocation to invoice {invoice.invoice_number}",
            invoice_id=invoice.id,
            payment_id=payment.id,
            service_id=invoice.service_id,
            reference_type="invoice_credit",
            reference_id=str(invoice.id),
            idempotency_key=idempotency_key or f"invoice-credit:{invoice.id}:{payment.id}",
            created_by_user_id=created_by_user_id,
        )
        if reseller is not None:
            ledger = CreditLedgerService.debit(db, reseller.id, amount_cents, **keywords)
        else:
            ledger = CreditLedgerService.debit_account(
                db, int(invoice.billing_account_id), amount_cents, **keywords
            )
        metadata = {"source": source, "ledger_entry_id": ledger.id}
        if idempotency_key is not None:
            metadata["idempotency_key"] = idempotency_key
        payment.payment_metadata = metadata
        InvoiceService.mark_paid_if_fully_allocated(db, invoice.id)
        if fire_event and credit_engine_mode(db) != "legacy":
            from app.services.billing import settlement

            settlement.fire(
                db,
                settlement.CREDIT_APPLIED,
                settlement.CreditApplied(
                    entry=ledger,
                    invoice=invoice,
                    balance_cents=int(ledger.balance_after_cents),
                    payment=payment,
                ),
            )
        return payment

    @staticmethod
    def apply_to_invoice(
        db: Session,
        invoice: Invoice,
        *,
        amount_cents: Optional[int] = None,
        actor_user_id: Optional[int] = None,
        idempotency_key: str,
    ) -> Optional[Payment]:
        """Pay (part of) an invoice from account credit as a ``credit`` payment (design 6.7).

        ``amount_cents`` defaults to ``min(available, remaining)``; an explicit
        amount must be positive and within both. Locks reseller, account and
        invoice in order. Returns the payment, the earlier payment for a
        repeated ``idempotency_key``, or None when there is nothing to apply.
        Retail invoices need the credit engine in ``v2``.
        """
        from app.services.billing_locks import BillingLocks

        if not idempotency_key:
            raise CreditLedgerError("idempotency_key is required")
        if amount_cents is not None:
            CreditLedgerService._require_integer_cents(amount_cents, positive=True)
        locked = BillingLocks.for_invoice(
            db, invoice.id, reseller=True, billing_account=True
        )
        if locked is None:
            raise CreditLedgerError(f"Invoice {invoice.id} was not found")
        existing = db.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.invoice_id != locked.id or existing.payment_id is None:
                raise CreditLedgerError(
                    "idempotency_key is already used by a different ledger operation"
                )
            return db.get(Payment, existing.payment_id)

        reseller: Optional[Reseller] = None
        if locked.reseller_id is not None:
            reseller = db.get(Reseller, locked.reseller_id)
            owner: Union[BillingAccount, Reseller] = reseller
        else:
            if locked.billing_account_id is None:
                raise CreditLedgerError("Invoice has no billing account")
            if credit_engine_mode(db) != "v2":
                raise RetailCreditUnavailableError(
                    "Retail account credit is available once the credit engine is v2"
                )
            owner = db.get(BillingAccount, locked.billing_account_id)
        if locked.status not in unpaid_invoice_statuses():
            raise CreditLedgerError("Invoice cannot take credit in its current state")

        available = CreditLedgerService.available_cents(db, owner)
        remaining = CreditLedgerService._remaining_cents(db, locked)
        if amount_cents is None:
            amount = min(available, remaining)
        else:
            if amount_cents > remaining:
                raise CreditLedgerError(
                    f"amount_cents exceeds the {remaining} cents due on the invoice"
                )
            if amount_cents > available:
                raise InsufficientCreditError(
                    reseller_id=locked.reseller_id,
                    billing_account_id=locked.billing_account_id,
                    available_cents=available,
                    requested_cents=amount_cents,
                )
            amount = amount_cents
        return CreditLedgerService.allocate_to_locked_invoice(
            db,
            locked,
            reseller=reseller,
            amount_cents=amount,
            created_by_user_id=actor_user_id,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def remove_from_invoice(
        db: Session,
        payment: Payment,
        *,
        amount_cents: int,
        actor_user_id: Optional[int],
        idempotency_key: str,
    ) -> Any:
        """Return applied credit to the balance as a ``credit`` refund (design 6.7).

        The refund (P1-R) credits the balance and reopens the invoice's due
        amount; no credit note is issued.
        """
        from app.models.refund import RefundDestination, RefundReason
        from app.services.refund_service import RefundService

        CreditLedgerService._require_integer_cents(amount_cents, positive=True)
        if not idempotency_key:
            raise CreditLedgerError("idempotency_key is required")
        if payment.gateway != CREDIT_PAYMENT_GATEWAY:
            raise CreditLedgerError("Only a credit payment can have its credit removed")
        invoice = db.get(Invoice, payment.invoice_id)
        if (
            invoice is not None
            and invoice.reseller_id is None
            and credit_engine_mode(db) != "v2"
        ):
            raise RetailCreditUnavailableError(
                "Retail account credit is available once the credit engine is v2"
            )
        return RefundService.request(
            db,
            payment_id=payment.id,
            amount_cents=amount_cents,
            destination=RefundDestination.CREDIT,
            reason=RefundReason.CREDIT_REMOVED,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            issue_credit_note=False,
        )
