"""A3 credit ledger: one chain per billing account, legacy/shadow/v2 modes (design 6)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_account import BillingAccount, SystemSetting
from app.models.reseller import (
    CreditLedgerEntry,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
)
from app.models.user import User
from app.services import billing_locks
from app.services.billing_shadow import SWITCH_RECORD_PREFIX, BillingShadowService
from app.services.credit_ledger_service import (
    CreditLedgerError,
    CreditLedgerService,
    InsufficientCreditError,
    RetailCreditUnavailableError,
    statuses_for_status_filter,
    unpaid_invoice_statuses,
)
from app.services.invoice_service import InvoiceService
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.settings_store import set_setting


def set_credit_mode(db, mode: str) -> None:
    set_setting(db, "billing_credit_engine", mode)
    key = SWITCH_RECORD_PREFIX + "credit"
    row = db.get(SystemSetting, key)
    if row is None:
        db.add(SystemSetting(key=key, value={"mode": mode}))
    else:
        row.value = {**row.value, "mode": mode}
    db.flush()


@pytest.fixture
def recorded(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        BillingShadowService, "record", staticmethod(lambda *args, **kwargs: calls.append(args))
    )
    return calls


def _reseller(db, name: str) -> Reseller:
    reseller = Reseller(user=User(username=f"{name}-r", email=f"{name}-r@example.com"))
    db.add(reseller)
    db.commit()
    return reseller


def _retail(db, name: str) -> BillingAccount:
    user = User(username=f"{name}-c", email=f"{name}-c@example.com")
    db.add(user)
    db.flush()
    account = BillingAccountDAO.ensure_client_account(db, user.id)
    db.commit()
    return account


def _account_of(db, reseller: Reseller) -> BillingAccount | None:
    return BillingAccountDAO.get_by_reseller_id(db, reseller.id)


def test_legacy_mode_writes_exactly_as_before(db_session, recorded):
    set_credit_mode(db_session, "legacy")
    reseller = _reseller(db_session, "legacy")
    entry = CreditLedgerService.credit(db_session, reseller.id, 1_000)
    CreditLedgerService.debit(db_session, reseller.id, 300)
    assert entry.billing_account_id is None
    assert reseller.cached_balance_cents == 700
    assert _account_of(db_session, reseller) is None
    assert recorded == []


def test_shadow_links_entries_and_moves_both_caches(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "shadow")
    first = CreditLedgerService.credit(db_session, reseller.id, 1_000)
    second = CreditLedgerService.debit(db_session, reseller.id, 400)
    account = _account_of(db_session, reseller)
    assert first.billing_account_id == second.billing_account_id == account.id
    assert (reseller.cached_balance_cents, account.credit_balance_cents) == (600, 600)
    assert [first.balance_after_cents, second.balance_after_cents] == [1_000, 600]
    assert recorded == []
    assert CreditLedgerService.balance_cents(db_session, account) == 600


def test_shadow_keeps_reseller_cache_authoritative_and_records_drift(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "drift")
    CreditLedgerService.credit(db_session, reseller.id, 500)
    account = _account_of(db_session, reseller)
    account.credit_balance_cents = 450
    entry = CreditLedgerService.debit(db_session, reseller.id, 100)
    assert entry.balance_after_cents == 400
    assert (reseller.cached_balance_cents, account.credit_balance_cents) == (400, 350)
    assert recorded == [("credit", "c1_cache_pair", "billing_account", account.id, 400, 350, f"ledger_entry:{entry.id}")]
    # The reseller cache decides whether a spend is affordable.
    with pytest.raises(InsufficientCreditError) as exc:
        CreditLedgerService.debit(db_session, reseller.id, 401)
    assert exc.value.available_cents == 400


def test_v2_makes_the_account_cache_authoritative(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "v2")
    CreditLedgerService.credit(db_session, reseller.id, 500)
    account = _account_of(db_session, reseller)
    reseller.cached_balance_cents = 900
    set_credit_mode(db_session, "v2")
    entry = CreditLedgerService.debit(db_session, reseller.id, 200)
    assert entry.balance_after_cents == 300 == account.credit_balance_cents
    assert reseller.cached_balance_cents == 700
    assert recorded and recorded[0][4:6] == (700, 300)
    assert CreditLedgerService.balance_cents(db_session, reseller) == 300


def test_retail_account_credit_chains_in_every_mode(db_session, recorded):
    set_credit_mode(db_session, "legacy")
    account = _retail(db_session, "retail")
    legacy = CreditLedgerService.credit_account(db_session, account.id, 100)
    assert legacy.billing_account_id == account.id and account.credit_balance_cents == 100
    CreditLedgerService.debit_account(db_session, account.id, 100)
    set_credit_mode(db_session, "shadow")
    first = CreditLedgerService.credit_account(db_session, account.id, 1_000, idempotency_key="r-1")
    again = CreditLedgerService.credit_account(db_session, account.id, 1_000, idempotency_key="r-1")
    debit = CreditLedgerService.debit_account(db_session, account.id, 250)
    adjust = CreditLedgerService.adjust_account(db_session, account.id, -50)
    assert again.id == first.id
    assert first.reseller_id is None and first.billing_account_id == account.id
    assert [first.balance_after_cents, debit.balance_after_cents, adjust.balance_after_cents] == [1_000, 750, 700]
    assert account.credit_balance_cents == 700
    with pytest.raises(InsufficientCreditError):
        CreditLedgerService.adjust_account(db_session, account.id, -701)
    with pytest.raises(CreditLedgerError):
        CreditLedgerService.debit_account(db_session, account.id, 1, idempotency_key="r-1")
    assert recorded == []


def test_account_methods_route_reseller_accounts_through_the_reseller(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "route")
    account = BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    entry = CreditLedgerService.credit_account(db_session, account.id, 300)
    assert entry.reseller_id == reseller.id
    assert reseller.cached_balance_cents == account.credit_balance_cents == 300


def test_available_is_never_negative(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    account = _retail(db_session, "negative")
    entry = CreditLedgerService.credit_account(db_session, account.id, 500)
    CreditLedgerService.debit_account(db_session, account.id, 400)
    CreditLedgerService.debit_account(db_session, account.id, 300, allow_negative_balance=True)
    assert entry.billing_account_id == account.id
    assert CreditLedgerService.balance_cents(db_session, account) == -200
    assert CreditLedgerService.available_cents(db_session, account) == 0


def test_reseller_write_locks_reseller_before_account(db_session, recorded, monkeypatch):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "order")
    BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    calls = []
    real = billing_locks._lock

    def spy(db, kind, row_id, statement, **kwargs):
        calls.append(kind)
        return real(db, kind, row_id, statement, **kwargs)

    monkeypatch.setattr(billing_locks, "_lock", spy)
    CreditLedgerService.credit(db_session, reseller.id, 10)
    assert calls[:2] == ["reseller", "billing_account"]


def test_new_reseller_account_without_entries_opens_at_zero(db_session):
    reseller = _reseller(db_session, "fresh")
    reseller.cached_balance_cents = 999
    account = BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    assert account.credit_balance_cents == 0


def test_new_reseller_account_with_entries_opens_at_the_reseller_cache(db_session):
    set_credit_mode(db_session, "legacy")
    reseller = _reseller(db_session, "fresh-entries")
    CreditLedgerService.credit(db_session, reseller.id, 400)
    assert BillingAccountDAO.get_by_reseller_id(db_session, reseller.id) is None
    account = BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    assert account.credit_balance_cents == 400


def _open_invoice(db, *, amount: int, reseller=None, account=None) -> Invoice:
    invoice = InvoiceService.create(
        db,
        reseller_id=reseller.id if reseller is not None else None,
        billing_account_id=account.id if account is not None else None,
        purpose=InvoicePurpose.CYCLE_CHARGE,
        amount_cents=amount,
    )
    invoice.status = InvoiceStatus.OPEN
    db.flush()
    return invoice


def test_apply_to_invoice_partial_then_idempotent(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "apply")
    CreditLedgerService.credit(db_session, reseller.id, 300)
    invoice = _open_invoice(db_session, amount=1_000, reseller=reseller)
    payment = CreditLedgerService.apply_to_invoice(db_session, invoice, idempotency_key="apply-1")
    again = CreditLedgerService.apply_to_invoice(db_session, invoice, idempotency_key="apply-1")
    assert again.id == payment.id
    assert (payment.gateway, payment.status, payment.amount_cents) == ("credit", PaymentStatus.SUCCEEDED, 300)
    assert reseller.cached_balance_cents == 0
    assert invoice.status == InvoiceStatus.OPEN
    entry = db_session.scalar(select(CreditLedgerEntry).where(CreditLedgerEntry.payment_id == payment.id))
    assert entry.amount_cents == -300 and entry.idempotency_key == "apply-1"
    # Nothing left to apply.
    assert CreditLedgerService.apply_to_invoice(db_session, invoice, idempotency_key="apply-2") is None


def test_apply_to_invoice_explicit_amount_checks(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "amount")
    CreditLedgerService.credit(db_session, reseller.id, 2_000)
    invoice = _open_invoice(db_session, amount=500, reseller=reseller)
    with pytest.raises(CreditLedgerError):
        CreditLedgerService.apply_to_invoice(db_session, invoice, amount_cents=600, idempotency_key="a-1")
    payment = CreditLedgerService.apply_to_invoice(db_session, invoice, amount_cents=500, idempotency_key="a-2")
    assert payment.amount_cents == 500
    assert invoice.status == InvoiceStatus.PAID
    with pytest.raises(CreditLedgerError):
        CreditLedgerService.apply_to_invoice(db_session, invoice, idempotency_key="a-3")
    poor = _reseller(db_session, "poor")
    other = _open_invoice(db_session, amount=500, reseller=poor)
    with pytest.raises(InsufficientCreditError):
        CreditLedgerService.apply_to_invoice(db_session, other, amount_cents=1, idempotency_key="a-4")


def test_apply_to_retail_invoice_needs_v2(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    account = _retail(db_session, "retailapply")
    CreditLedgerService.credit_account(db_session, account.id, 800)
    invoice = _open_invoice(db_session, amount=500, account=account)
    with pytest.raises(RetailCreditUnavailableError):
        CreditLedgerService.apply_to_invoice(db_session, invoice, idempotency_key="ra-1")
    set_credit_mode(db_session, "v2")
    payment = CreditLedgerService.apply_to_invoice(db_session, invoice, idempotency_key="ra-2")
    assert payment.amount_cents == 500
    assert account.credit_balance_cents == 300
    assert invoice.status == InvoiceStatus.PAID


def test_remove_from_invoice_requests_a_credit_refund(db_session, recorded, monkeypatch):
    from app.models.refund import RefundDestination, RefundReason
    from app.services import refund_service

    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "remove")
    CreditLedgerService.credit(db_session, reseller.id, 400)
    invoice = _open_invoice(db_session, amount=400, reseller=reseller)
    payment = CreditLedgerService.apply_to_invoice(db_session, invoice, idempotency_key="rm-apply")
    calls = []
    monkeypatch.setattr(
        refund_service.RefundService,
        "request",
        staticmethod(lambda db, **kwargs: calls.append(kwargs) or SimpleNamespace(id=1, **kwargs)),
    )
    refund = CreditLedgerService.remove_from_invoice(
        db_session, payment, amount_cents=150, actor_user_id=None, idempotency_key="rm-1"
    )
    assert refund.id == 1
    assert calls == [
        {
            "payment_id": payment.id,
            "amount_cents": 150,
            "destination": RefundDestination.CREDIT,
            "reason": RefundReason.CREDIT_REMOVED,
            "actor_user_id": None,
            "idempotency_key": "rm-1",
            "issue_credit_note": False,
        }
    ]
    card = Payment(invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED, amount_cents=1, currency="USD")
    with pytest.raises(CreditLedgerError):
        CreditLedgerService.remove_from_invoice(db_session, card, amount_cents=1, actor_user_id=None, idempotency_key="rm-2")


def test_negative_balance_is_refused_by_default(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "nodebt")
    CreditLedgerService.credit(db_session, reseller.id, 100)
    account = _account_of(db_session, reseller)
    invoice = _open_invoice(db_session, amount=500, reseller=reseller)
    with pytest.raises(InsufficientCreditError):
        CreditLedgerService.apply_to_invoice(db_session, invoice, amount_cents=300, idempotency_key="nd-1")
    with pytest.raises(InsufficientCreditError):
        CreditLedgerService.debit_account(db_session, account.id, 101)
    with pytest.raises(InsufficientCreditError):
        CreditLedgerService.adjust_account(db_session, account.id, -101)
    retail = _retail(db_session, "nodebt")
    CreditLedgerService.credit_account(db_session, retail.id, 50)
    with pytest.raises(InsufficientCreditError):
        CreditLedgerService.debit_account(db_session, retail.id, 51)
    assert (reseller.cached_balance_cents, account.credit_balance_cents) == (100, 100)
    assert retail.credit_balance_cents == 50


@pytest.mark.parametrize("mode", ["shadow", "v2"])
def test_apply_to_invoice_may_take_a_reseller_negative(db_session, recorded, mode):
    set_credit_mode(db_session, mode)
    reseller = _reseller(db_session, f"debt{mode}")
    CreditLedgerService.credit(db_session, reseller.id, 100)
    account = _account_of(db_session, reseller)
    invoice = _open_invoice(db_session, amount=500, reseller=reseller)
    payment = CreditLedgerService.apply_to_invoice(
        db_session, invoice, idempotency_key=f"neg-{mode}", allow_negative_balance=True
    )
    again = CreditLedgerService.apply_to_invoice(
        db_session, invoice, idempotency_key=f"neg-{mode}", allow_negative_balance=True
    )
    assert again.id == payment.id
    assert payment.amount_cents == 500
    assert invoice.status == InvoiceStatus.PAID
    # Both caches move by exactly the amount and agree, so no drift is recorded.
    assert (reseller.cached_balance_cents, account.credit_balance_cents) == (-400, -400)
    assert CreditLedgerService.balance_cents(db_session, account) == -400
    assert CreditLedgerService.available_cents(db_session, account) == 0
    assert recorded == []


def test_explicit_amount_beyond_credit_with_negative_allowed(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "debtexplicit")
    invoice = _open_invoice(db_session, amount=500, reseller=reseller)
    payment = CreditLedgerService.apply_to_invoice(
        db_session, invoice, amount_cents=200, idempotency_key="ne-1", allow_negative_balance=True
    )
    assert payment.amount_cents == 200
    assert reseller.cached_balance_cents == _account_of(db_session, reseller).credit_balance_cents == -200
    # The invoice's remaining amount still caps the application.
    with pytest.raises(CreditLedgerError):
        CreditLedgerService.apply_to_invoice(
            db_session, invoice, amount_cents=301, idempotency_key="ne-2", allow_negative_balance=True
        )


def test_retail_account_may_go_negative_when_allowed(db_session, recorded):
    set_credit_mode(db_session, "v2")
    retail = _retail(db_session, "retaildebt")
    CreditLedgerService.credit_account(db_session, retail.id, 100)
    invoice = _open_invoice(db_session, amount=300, account=retail)
    payment = CreditLedgerService.apply_to_invoice(
        db_session, invoice, idempotency_key="rd-1", allow_negative_balance=True
    )
    assert payment.amount_cents == 300
    assert retail.credit_balance_cents == -200
    CreditLedgerService.debit_account(db_session, retail.id, 50, allow_negative_balance=True)
    CreditLedgerService.adjust_account(db_session, retail.id, -25, allow_negative_balance=True)
    assert retail.credit_balance_cents == -275


def test_account_debit_may_take_a_reseller_negative_in_shadow(db_session, recorded):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "acctdebt")
    account = BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    CreditLedgerService.credit_account(db_session, account.id, 100)
    debit = CreditLedgerService.debit_account(db_session, account.id, 250, allow_negative_balance=True)
    adjust = CreditLedgerService.adjust_account(db_session, account.id, -50, allow_negative_balance=True)
    assert [debit.balance_after_cents, adjust.balance_after_cents] == [-150, -200]
    assert (reseller.cached_balance_cents, account.credit_balance_cents) == (-200, -200)
    assert recorded == []


def test_paid_retail_topup_credits_the_account(db_session, recorded):
    set_credit_mode(db_session, "v2")
    account = _retail(db_session, "topup")
    invoice = InvoiceService.create(
        db_session, billing_account_id=account.id, purpose=InvoicePurpose.CREDIT_TOPUP, amount_cents=2_500
    )
    invoice.status = InvoiceStatus.OPEN
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.PENDING,
        amount_cents=2_500, currency="USD", external_ref="pi_topup_1",
    )
    db_session.add(payment)
    db_session.flush()
    PaymentOrchestrator.finalize_success(db_session, payment)
    PaymentOrchestrator.finalize_success(db_session, payment)
    assert account.credit_balance_cents == 2_500
    entries = list(db_session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.billing_account_id == account.id)))
    assert [entry.amount_cents for entry in entries] == [2_500]


def test_status_helpers():
    unpaid = set(unpaid_invoice_statuses())
    assert {InvoiceStatus.OPEN, InvoiceStatus.PENDING_ACTION, InvoiceStatus.OVERDUE} <= unpaid
    assert InvoiceStatus.PAID not in unpaid and InvoiceStatus.VOID not in unpaid
    assert set(statuses_for_status_filter(InvoiceStatus.OPEN)) >= {InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID}
    assert set(statuses_for_status_filter(InvoiceStatus.PAID)) == {InvoiceStatus.PAID, InvoiceStatus.REFUNDED}
    assert statuses_for_status_filter(InvoiceStatus.REFUNDED) == (InvoiceStatus.REFUNDED,)
