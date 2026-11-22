"""P1-R: refund, dispute, top-up hold and credit refund paths follow LOCK_ORDER with the billing account.

Uses W0e's lock recorder. A gateway refund commits its reservation before the
gateway call, so the recorded sequence is checked per transaction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from app.dao.commerce_dao import BillingAccountDAO
from app.models.refund import RefundDestination, RefundStatus
from app.models.reseller import InvoicePurpose
from app.services.payments.base import GatewayWebhookAction, NormalizedGatewayWebhookEvent
from app.services.payments.refunds import PaymentRefundService
from app.services.payments.webhooks import PaymentWebhookService
from tests.services.test_billing_p1r_refund_service import (  # noqa: F401
    FakeGateway,
    _paid,
    _registry,
    _request,
    _reseller,
    _retail_account,
    v2,
)
from tests.services.test_billing_w0e_review_fixes import _assert_lock_order, lock_calls  # noqa: F401


@pytest.fixture
def transactions(db_session, lock_calls):  # noqa: F811
    """The recorded lock calls, split at every commit and rollback."""

    def mark(_session):
        lock_calls.append(("<end>", None))

    event.listen(db_session, "after_commit", mark)
    event.listen(db_session, "after_rollback", mark)

    def split():
        segments, current = [], []
        for call in lock_calls:
            if call[0] == "<end>":
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(call)
        if current:
            segments.append(current)
        return segments

    yield split
    event.remove(db_session, "after_commit", mark)
    event.remove(db_session, "after_rollback", mark)


def _check(segments):
    assert segments, "no locks were recorded"
    for segment in segments:
        _assert_lock_order(segment)
        kinds = [kind for kind, _ in segment]
        if "invoice" in kinds:
            assert "billing_account" in kinds[: kinds.index("invoice")], segment


def _reseller_with_account(db, suffix):
    reseller = _reseller(db, suffix)
    BillingAccountDAO.ensure_reseller_account(db, reseller)
    db.commit()
    return reseller


def test_admin_refund_reversal_locks_billing_account(db_session, lock_calls, transactions):  # noqa: F811
    _invoice, payment = _paid(db_session, reseller=_reseller_with_account(db_session, "lk-admin"))
    lock_calls.clear()
    PaymentRefundService(registry=_registry(FakeGateway())).refund(db_session, payment.id, reason="test")
    db_session.commit()
    segments = transactions()
    assert len(segments) >= 2  # reservation, then completion after the gateway call
    _check(segments)
    last = segments[-1]
    assert [kind for kind, _ in last[:2]] == ["reseller", "billing_account"]
    # The ledger reversal re-takes the same reseller and account rows, already held.
    assert {call for call in last[2:] if call[0] in ("reseller", "billing_account")} <= set(last[:2])


def test_dispute_reversal_locks_billing_account(db_session, lock_calls, transactions):  # noqa: F811
    _invoice, _payment = _paid(db_session, reseller=_reseller_with_account(db_session, "lk-disp"), ref="pi_lk_disp")
    lock_calls.clear()
    dispute = NormalizedGatewayWebhookEvent(
        event_id="evt_lk_disp", event_type="charge.dispute.created",
        action=GatewayWebhookAction.PAYMENT_DISPUTED, external_ref="pi_lk_disp",
    )
    PaymentWebhookService.process(db_session, gateway="stripe", event=dispute, raw_payload=b"{}")
    db_session.commit()
    _check(transactions())


def test_usdt_settlement_locks_billing_account_before_invoice(db_session, lock_calls, transactions):  # noqa: F811
    from app.services.payments.usdt_watcher import UsdtWatcher
    from tests.services.test_usdt_watcher import FakeRpc, _config, _deposit, _log

    reseller, invoice, _deposit_row, payment, _cursor = _deposit(db_session)
    BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    db_session.commit()
    lock_calls.clear()
    UsdtWatcher(FakeRpc([_log(1_000_000, tx_digit="a", log_index=0)]), _config()).run_once(db_session)
    db_session.commit()
    _check(transactions())
    first = [call for call in lock_calls if call[0] in ("reseller", "billing_account", "invoice", "payment")][:4]
    assert [kind for kind, _ in first] == ["reseller", "billing_account", "invoice", "payment"]
    assert first[2] == ("invoice", invoice.id)
    db_session.refresh(payment)
    assert payment.status.value == "succeeded"


def test_v2_topup_hold_locks_billing_account(db_session, lock_calls, transactions, v2):  # noqa: F811
    account = _retail_account(db_session, "lk-topup")
    _invoice, payment = _paid(db_session, account=account, purpose=InvoicePurpose.CREDIT_TOPUP)
    lock_calls.clear()
    refund = _request(db_session, payment, FakeGateway(), key="lk-topup")
    db_session.commit()
    assert refund.status == RefundStatus.SUCCEEDED
    assert v2.debit  # the hold was written
    _check(transactions())


def test_v2_credit_refund_locks_billing_account(db_session, lock_calls, transactions, v2):  # noqa: F811
    account = _retail_account(db_session, "lk-credit")
    _invoice, payment = _paid(db_session, account=account)
    lock_calls.clear()
    refund = _request(db_session, payment, FakeGateway(), amount=500, key="lk-credit", destination=RefundDestination.CREDIT)
    db_session.commit()
    assert refund.status == RefundStatus.SUCCEEDED and v2.credit
    _check(transactions())
