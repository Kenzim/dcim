import pytest
from sqlalchemy import select

from app.models.reseller import (
    CreditLedgerEntry,
    CreditLedgerEntryType,
    Reseller,
)
from app.models.user import User
from app.services.credit_ledger_service import (
    CreditLedgerError,
    CreditLedgerService,
    InsufficientCreditError,
    LedgerEntryAlreadyReversedError,
)


def _reseller(db_session, suffix: str = "ledger") -> Reseller:
    reseller = Reseller(
        user=User(
            username=f"{suffix}-reseller",
            email=f"{suffix}-reseller@example.com",
        )
    )
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
    return reseller


def test_credit_debit_adjust_and_reverse_append_balances(db_session):
    reseller = _reseller(db_session)

    credit = CreditLedgerService.credit(
        db_session, reseller.id, 1000, description="Initial credit"
    )
    debit = CreditLedgerService.debit(
        db_session, reseller.id, 250, description="Service charge"
    )
    adjustment = CreditLedgerService.adjust(
        db_session, reseller.id, 50, description="Courtesy credit"
    )
    reversal = CreditLedgerService.reverse(
        db_session, reseller.id, debit.id
    )

    assert [
        credit.amount_cents,
        debit.amount_cents,
        adjustment.amount_cents,
        reversal.amount_cents,
    ] == [1000, -250, 50, 250]
    assert [
        credit.balance_after_cents,
        debit.balance_after_cents,
        adjustment.balance_after_cents,
        reversal.balance_after_cents,
    ] == [1000, 750, 800, 1050]
    assert reversal.entry_type == CreditLedgerEntryType.REVERSAL
    assert reversal.reversal_of_id == debit.id
    assert reseller.cached_balance_cents == 1050

    with pytest.raises(LedgerEntryAlreadyReversedError):
        CreditLedgerService.reverse(db_session, reseller.id, debit.id)


def test_debit_rejects_insufficient_funds_without_partial_entry(db_session):
    reseller = _reseller(db_session, "insufficient")
    CreditLedgerService.credit(db_session, reseller.id, 100)

    with pytest.raises(InsufficientCreditError) as exc_info:
        CreditLedgerService.debit(db_session, reseller.id, 101)

    assert exc_info.value.available_cents == 100
    assert exc_info.value.requested_cents == 101
    assert reseller.cached_balance_cents == 100
    assert (
        db_session.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.reseller_id == reseller.id
            )
        )
        .scalars()
        .all()
    ) == [reseller.ledger_entries[0]]


def test_topup_reversal_can_create_negative_balance(db_session):
    reseller = _reseller(db_session, "chargeback")
    topup = CreditLedgerService.credit(db_session, reseller.id, 100)
    CreditLedgerService.debit(db_session, reseller.id, 75)

    reversal = CreditLedgerService.reverse(db_session, reseller.id, topup.id)

    assert reversal.amount_cents == -100
    assert reversal.balance_after_cents == -75
    assert reseller.cached_balance_cents == -75
    with pytest.raises(InsufficientCreditError):
        CreditLedgerService.debit(db_session, reseller.id, 1)


def test_idempotency_key_does_not_apply_credit_twice(db_session):
    reseller = _reseller(db_session, "idempotent")

    first = CreditLedgerService.credit(
        db_session, reseller.id, 500, idempotency_key="topup-123"
    )
    retry = CreditLedgerService.credit(
        db_session, reseller.id, 500, idempotency_key="topup-123"
    )

    assert retry is first
    assert reseller.cached_balance_cents == 500
    assert len(reseller.ledger_entries) == 1

    with pytest.raises(CreditLedgerError, match="different ledger operation"):
        CreditLedgerService.credit(
            db_session, reseller.id, 501, idempotency_key="topup-123"
        )


@pytest.mark.parametrize("invalid_amount", [1.5, "100", True, 0])
def test_credit_rejects_non_integer_or_nonpositive_amount(
    db_session, invalid_amount
):
    reseller = _reseller(db_session, f"invalid-{str(invalid_amount)}")

    with pytest.raises(CreditLedgerError):
        CreditLedgerService.credit(
            db_session, reseller.id, invalid_amount
        )


def test_ledger_flushes_without_committing_and_caller_can_rollback(db_session):
    reseller = _reseller(db_session, "rollback")

    entry = CreditLedgerService.credit(db_session, reseller.id, 500)
    assert entry.id is not None
    assert reseller.cached_balance_cents == 500
    assert db_session.in_transaction()

    db_session.rollback()

    persisted_reseller = db_session.get(Reseller, reseller.id)
    assert persisted_reseller.cached_balance_cents == 0
    assert (
        db_session.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.reseller_id == reseller.id
            )
        )
        .scalars()
        .all()
        == []
    )
