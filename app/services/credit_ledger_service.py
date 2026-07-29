"""Transactional operations for the reseller credit ledger.

Every public method flushes its changes but deliberately leaves commit and
rollback ownership with the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dao.reseller_dao import ResellerDAO
from app.models.reseller import (
    CreditLedgerEntry,
    CreditLedgerEntryType,
    Reseller,
)


class CreditLedgerError(ValueError):
    """Base exception for rejected credit ledger operations."""


class ResellerNotFoundError(CreditLedgerError):
    pass


class InsufficientCreditError(CreditLedgerError):
    def __init__(
        self, *, reseller_id: int, available_cents: int, requested_cents: int
    ) -> None:
        self.reseller_id = reseller_id
        self.available_cents = available_cents
        self.requested_cents = requested_cents
        super().__init__(
            f"Reseller {reseller_id} has {available_cents} cents available; "
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
    def _append_locked(
        db: Session,
        *,
        reseller: Reseller,
        entry_type: CreditLedgerEntryType,
        signed_amount_cents: int,
        context: _LedgerEntryContext,
        allow_negative_balance: bool = False,
    ) -> CreditLedgerEntry:
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
            description=context.description,
            invoice_id=context.invoice_id,
            payment_id=context.payment_id,
            service_id=context.service_id,
            reversal_of_id=context.reversal_of_id,
            reference_type=context.reference_type,
            reference_id=context.reference_id,
            idempotency_key=context.idempotency_key,
            ledger_metadata=dict(context.metadata or {}),
            created_by_user_id=context.created_by_user_id,
        )
        db.add(entry)
        db.flush()
        return entry

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
        reseller = CreditLedgerService._lock_reseller(db, reseller_id)
        return CreditLedgerService._append_locked(
            db,
            reseller=reseller,
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
        reseller = CreditLedgerService._lock_reseller(db, reseller_id)
        return CreditLedgerService._append_locked(
            db,
            reseller=reseller,
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
        reseller = CreditLedgerService._lock_reseller(db, reseller_id)
        return CreditLedgerService._append_locked(
            db,
            reseller=reseller,
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
        reseller = CreditLedgerService._lock_reseller(db, reseller_id)
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

        return CreditLedgerService._append_locked(
            db,
            reseller=reseller,
            entry_type=CreditLedgerEntryType.REVERSAL,
            signed_amount_cents=-original.amount_cents,
            # Gateway chargebacks and top-up reversals can legitimately put
            # the account into debt. Deploy/cycle debits still reject an
            # insufficient balance; a negative balance blocks future spends
            # until a later credit restores it.
            allow_negative_balance=True,
            context=_LedgerEntryContext(
                description=description
                or f"Reversal of ledger entry {original.id}",
                invoice_id=original.invoice_id,
                payment_id=original.payment_id,
                service_id=original.service_id,
                reversal_of_id=original.id,
                reference_type=original.reference_type,
                reference_id=original.reference_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
                created_by_user_id=created_by_user_id,
            ),
        )
