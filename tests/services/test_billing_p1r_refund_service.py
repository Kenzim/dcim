"""P1-R: RefundService, the refund reconciler, refund and dispute webhooks, gateway adapters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_audit import UserAuditEvent
from app.models.refund import Refund, RefundDestination, RefundReason, RefundStatus
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
from app.services import refund_service as refund_module
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_adjustment_service import InvoiceAdjustmentService
from app.services.invoice_service import InvoiceService
from app.services.payments.base import (
    GatewayRefundResult,
    GatewayResultStatus,
    GatewayWebhookAction,
    NormalizedGatewayWebhookEvent,
)
from app.services.payments.refund_reconciler import RefundReconciler
from app.services.payments.registry import PaymentGatewayRegistry
from app.services.payments.webhooks import PaymentWebhookService
from app.services.refund_service import RefundError, RefundService

OK = GatewayRefundResult(status=GatewayResultStatus.SUCCEEDED, external_ref="re_ok")
PENDING = GatewayRefundResult(status=GatewayResultStatus.SUCCEEDED, external_ref="re_pend", pending=True)
DECLINED = GatewayRefundResult(
    status=GatewayResultStatus.DECLINED, failure_code="card_closed", failure_message="Closed"
)
TIMEOUT = GatewayRefundResult(status=GatewayResultStatus.TRANSIENT_ERROR, failure_code="Timeout")


class FakeGateway:
    def __init__(self, db=None, outcome=OK, lookup=None, name="stripe"):
        self.name = name
        self.db = db
        self.outcome = outcome
        self.lookup = lookup
        self.calls = []
        self.lookups = []
        self.in_transaction_during_call = []

    def refund_payment(self, **kwargs):
        if self.db is not None:
            self.in_transaction_during_call.append(self.db.in_transaction())
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def lookup_refund(self, **kwargs):
        self.lookups.append(kwargs)
        return self.lookup


def _registry(gateway):
    registry = PaymentGatewayRegistry()
    registry.register(gateway)
    return registry


def _reseller(db, suffix):
    reseller = Reseller(
        user=User(username=f"p1r-{suffix}", email=f"p1r-{suffix}@example.test", is_reseller=True)
    )
    db.add(reseller)
    db.flush()
    return reseller


_NUMBER = [960000]


def _paid(db, *, reseller=None, account=None, gateway="stripe", amount=2000, purpose=InvoicePurpose.DEPLOY_CHARGE, ref=None):
    _NUMBER[0] += 1
    invoice = Invoice(
        invoice_number=_NUMBER[0],
        reseller_id=reseller.id if reseller is not None else None,
        billing_account_id=account.id if account is not None else None,
        purpose=purpose,
        status=InvoiceStatus.PAID,
        amount_cents=amount,
        currency="USD",
    )
    db.add(invoice)
    db.flush()
    payment = Payment(
        invoice_id=invoice.id,
        gateway=gateway,
        status=PaymentStatus.SUCCEEDED,
        amount_cents=amount,
        currency="USD",
        external_ref=ref or f"pi_{invoice.id}",
    )
    db.add(payment)
    db.commit()
    return invoice, payment


def _retail_account(db, suffix):
    user = User(username=f"p1r-retail-{suffix}", email=f"p1r-retail-{suffix}@example.test")
    db.add(user)
    db.flush()
    return BillingAccountDAO.ensure_client_account(db, user.id)


def _request(db, payment, gateway, *, amount=None, key="k1", destination=RefundDestination.GATEWAY, **kw):
    return RefundService.request(
        db,
        payment_id=payment.id,
        amount_cents=amount if amount is not None else payment.amount_cents,
        destination=destination,
        reason=kw.pop("reason", RefundReason.CUSTOMER_REQUEST),
        actor_user_id=None,
        idempotency_key=key,
        registry=_registry(gateway),
        **kw,
    )


# --------------------------------------------------------------- legacy mode


def test_gateway_success_writes_refunded_cents_and_today_side_effects(db_session):
    reseller = _reseller(db_session, "ok")
    invoice, payment = _paid(db_session, reseller=reseller)
    gateway = FakeGateway(db_session)

    refund = _request(db_session, payment, gateway)
    db_session.commit()
    db_session.refresh(payment)
    db_session.refresh(invoice)

    assert refund.status == RefundStatus.SUCCEEDED
    assert refund.gateway_refund_ref == "re_ok"
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.refunded_cents == 2000
    assert invoice.status == InvoiceStatus.FAILED
    assert gateway.calls[0]["idempotency_key"] == "k1"
    # The pending row was committed before the call: no transaction, so no lock, was open.
    assert gateway.in_transaction_during_call == [False]
    assert db_session.scalar(select(CreditLedgerEntry.amount_cents).where(CreditLedgerEntry.payment_id == payment.id)) == -2000


def test_gateway_decline_fails_refund_and_leaves_payment(db_session):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "decl"))
    refund = _request(db_session, payment, FakeGateway(outcome=DECLINED))
    db_session.commit()
    db_session.refresh(payment)
    assert refund.status == RefundStatus.FAILED
    assert refund.failure_code == "card_closed"
    assert payment.status == PaymentStatus.SUCCEEDED
    assert payment.refunded_cents == 0


@pytest.mark.parametrize("outcome", [TIMEOUT, PENDING, RuntimeError("socket closed")])
def test_no_definitive_answer_leaves_refund_pending(db_session, outcome):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, f"pend{id(outcome)}"))
    refund = _request(db_session, payment, FakeGateway(outcome=outcome))
    db_session.commit()
    db_session.refresh(payment)
    assert refund.status == RefundStatus.PENDING
    assert refund.next_attempt_at is not None
    assert refund.claim_token is None
    assert payment.status == PaymentStatus.SUCCEEDED
    assert refund_module.refundable_cents(db_session, payment) == 0


def test_idempotency_key_replays_and_conflicts(db_session):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "idem"))
    gateway = FakeGateway()
    first = _request(db_session, payment, gateway)
    db_session.commit()
    again = _request(db_session, payment, gateway)
    assert again.id == first.id
    assert len(gateway.calls) == 1
    with pytest.raises(RefundError) as exc:
        _request(db_session, payment, gateway, amount=1000)
    assert exc.value.code == "idempotency_conflict"


def test_legacy_refuses_partial_manual_and_credit_destinations(db_session):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "legacy"))
    gateway = FakeGateway()
    with pytest.raises(RefundError) as exc:
        _request(db_session, payment, gateway, amount=500)
    assert exc.value.code == "requires_v2"
    with pytest.raises(RefundError) as exc:
        _request(db_session, payment, gateway, destination=RefundDestination.MANUAL, key="k2")
    assert exc.value.code == "requires_v2"
    with pytest.raises(RefundError) as exc:
        _request(db_session, payment, gateway, destination=RefundDestination.CREDIT, key="k3")
    assert exc.value.code == "requires_v2"


def test_usdt_gateway_refund_refused(db_session):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "usdt"), gateway="usdt")
    with pytest.raises(RefundError) as exc:
        _request(db_session, payment, FakeGateway(name="usdt"))
    assert exc.value.code == "not_refundable"


def test_amount_above_refundable_refused(db_session, monkeypatch):
    monkeypatch.setattr(refund_module, "invoice_engine_mode", lambda db: "v2")
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "over"))
    with pytest.raises(RefundError) as exc:
        _request(db_session, payment, FakeGateway(), amount=2001)
    assert exc.value.code == "amount_exceeds_refundable"


# ------------------------------------------------------------------ v2 mode


@pytest.fixture
def v2(monkeypatch):
    calls = SimpleNamespace(recompute=[], credit=[], debit=[], apply=[], notes=[])
    monkeypatch.setattr(refund_module, "invoice_engine_mode", lambda db: "v2")
    monkeypatch.setattr(InvoiceService, "recompute", staticmethod(lambda db, invoice, *, cause: calls.recompute.append((invoice.id, cause)) or invoice))
    monkeypatch.setattr(
        CreditLedgerService,
        "credit_account",
        staticmethod(lambda db, account_id, amount, **kw: calls.credit.append((account_id, amount, kw)) or SimpleNamespace(id=700 + len(calls.credit))),
    )
    monkeypatch.setattr(
        CreditLedgerService,
        "debit_account",
        staticmethod(lambda db, account_id, amount, **kw: calls.debit.append((account_id, amount, kw)) or SimpleNamespace(id=800 + len(calls.debit))),
    )
    monkeypatch.setattr(
        CreditLedgerService,
        "apply_to_invoice",
        staticmethod(lambda db, invoice, **kw: calls.apply.append((invoice.id, kw)) or SimpleNamespace(id=900)),
    )
    monkeypatch.setattr(
        InvoiceAdjustmentService,
        "issue_for_refund",
        staticmethod(lambda db, refund, *, lines=None: calls.notes.append((refund.id, lines)) or SimpleNamespace(id=600 + len(calls.notes))),
    )
    return calls


def test_v2_partial_gateway_refund_issues_credit_note_and_recomputes(db_session, v2):
    account = _retail_account(db_session, "partial")
    invoice, payment = _paid(db_session, account=account)
    refund = _request(db_session, payment, FakeGateway(), amount=500)
    db_session.commit()
    db_session.refresh(payment)
    assert refund.status == RefundStatus.SUCCEEDED
    assert payment.status == PaymentStatus.PARTIALLY_REFUNDED
    assert payment.refunded_cents == 500
    assert refund.adjustment_id == 601
    assert [note[0] for note in v2.notes] == [refund.id]
    assert v2.recompute == [(invoice.id, "refund")]
    second = _request(db_session, payment, FakeGateway(outcome=GatewayRefundResult(status=GatewayResultStatus.SUCCEEDED, external_ref="re_2")), amount=1500, key="k2")
    db_session.commit()
    db_session.refresh(payment)
    assert second.status == RefundStatus.SUCCEEDED
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.refunded_cents == 2000


@pytest.mark.parametrize(
    "total,tax,parts",
    [
        # Per refund on its own each part rounds to 56: 168 refunded against 167.
        (1000, 167, [333, 333, 334]),
        # Per refund on its own each 1-cent part rounds to 0: no tax refunded at all.
        (10, 3, [1] * 10),
        (997, 143, [101, 250, 1, 399, 246]),
    ],
)
def test_v2_partial_refund_tax_is_cumulative(db_session, v2, total, tax, parts):
    account = _retail_account(db_session, f"tax{total}{len(parts)}")
    invoice, payment = _paid(db_session, account=account, amount=total)
    invoice.tax_cents = tax
    db_session.commit()
    taxes = []
    for index, part in enumerate(parts):
        ref = GatewayRefundResult(status=GatewayResultStatus.SUCCEEDED, external_ref=f"re_tax_{total}_{index}")
        refund = _request(db_session, payment, FakeGateway(outcome=ref), amount=part, key=f"tax-{total}-{index}")
        db_session.commit()
        assert refund.status == RefundStatus.SUCCEEDED
        taxes.append(refund.refund_metadata["tax_cents"])
    assert sum(taxes) == tax
    assert all(0 <= value <= part for value, part in zip(taxes, parts))
    # The credit note lines carry the same split: net plus tax equals each refund.
    notes = v2.notes[-len(parts):]
    assert [line[0]["tax_cents"] for _refund_id, line in notes] == taxes
    assert [line[0]["unit_cents"] + line[0]["tax_cents"] for _refund_id, line in notes] == parts


def test_v2_refund_to_credit_credits_account_without_gateway(db_session, v2):
    account = _retail_account(db_session, "credit")
    _invoice, payment = _paid(db_session, account=account)
    gateway = FakeGateway()
    refund = _request(db_session, payment, gateway, amount=700, destination=RefundDestination.CREDIT)
    assert refund.status == RefundStatus.SUCCEEDED
    assert gateway.calls == []
    assert v2.credit[0][:2] == (account.id, 700)
    assert v2.credit[0][2]["idempotency_key"] == f"refund:{refund.id}"
    assert refund.ledger_entry_id == 701


def test_v2_topup_hold_placed_and_released_on_decline(db_session, v2):
    account = _retail_account(db_session, "topup")
    _invoice, payment = _paid(db_session, account=account, purpose=InvoicePurpose.CREDIT_TOPUP)
    refund = _request(db_session, payment, FakeGateway(outcome=DECLINED))
    assert refund.status == RefundStatus.FAILED
    assert v2.debit[0][2]["idempotency_key"] == f"refund-hold:{refund.id}"
    assert v2.credit[0][2]["idempotency_key"] == f"refund-hold-release:{refund.id}"
    assert v2.notes == []


def test_v2_retail_dispute_reopens_without_credit_note(db_session, v2):
    account = _retail_account(db_session, "dispute")
    invoice, payment = _paid(db_session, account=account, ref="pi_disp")
    event = NormalizedGatewayWebhookEvent(
        event_id="evt_disp", event_type="charge.dispute.created",
        action=GatewayWebhookAction.PAYMENT_DISPUTED, external_ref="pi_disp", amount_cents=2000,
    )
    PaymentWebhookService.process(db_session, gateway="stripe", event=event, raw_payload=b"{}")
    db_session.commit()
    refund = db_session.execute(select(Refund).where(Refund.payment_id == payment.id)).scalar_one()
    assert refund.reason == RefundReason.DISPUTE
    assert refund.status == RefundStatus.SUCCEEDED
    assert refund.issue_credit_note is False
    assert refund.idempotency_key == "stripe-dispute:evt_disp"
    assert refund.refund_metadata["dispute_policy"] == "reopen_invoice"
    assert v2.notes == [] and v2.apply == []
    assert v2.recompute == [(invoice.id, "dispute")]


def test_v2_reseller_dispute_debit_balance_applies_credit(db_session, v2):
    reseller = _reseller(db_session, "rdisp")
    invoice, payment = _paid(db_session, reseller=reseller, ref="pi_rdisp")
    BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    db_session.commit()
    event = NormalizedGatewayWebhookEvent(
        event_id="evt_rdisp", event_type="charge.dispute.created",
        action=GatewayWebhookAction.PAYMENT_DISPUTED, external_ref="pi_rdisp",
    )
    PaymentWebhookService.process(db_session, gateway="stripe", event=event, raw_payload=b"{}")
    refund = db_session.execute(select(Refund).where(Refund.payment_id == payment.id)).scalar_one()
    assert v2.apply == [(invoice.id, {
        "amount_cents": 2000, "actor_user_id": None,
        "idempotency_key": f"dispute-apply:{refund.id}", "allow_negative_balance": True,
    })]


# ---------------------------------------------------------------- webhooks


def _refund_event(event_id, ref, *, refund_ref=None, status="succeeded", amount=None):
    return NormalizedGatewayWebhookEvent(
        event_id=event_id, event_type="charge.refunded", action=GatewayWebhookAction.PAYMENT_REVERSED,
        external_ref=ref, amount_cents=amount, refund_ref=refund_ref, refund_status=status,
    )


def test_webhook_completes_pending_refund(db_session):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "whpend"), ref="pi_whp")
    refund = _request(db_session, payment, FakeGateway(outcome=PENDING))
    db_session.commit()
    assert refund.status == RefundStatus.PENDING
    PaymentWebhookService.process(db_session, gateway="stripe", event=_refund_event("evt_1", "pi_whp", refund_ref="re_pend"), raw_payload=b"{}")
    db_session.commit()
    db_session.refresh(refund)
    db_session.refresh(payment)
    assert refund.status == RefundStatus.SUCCEEDED
    assert payment.refunded_cents == 2000
    assert db_session.scalar(select(Refund.id).where(Refund.payment_id == payment.id, Refund.id != refund.id)) is None


def test_webhook_failed_refund_fails_pending_row(db_session):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "whfail"), ref="pi_whf")
    refund = _request(db_session, payment, FakeGateway(outcome=PENDING))
    db_session.commit()
    PaymentWebhookService.process(db_session, gateway="stripe", event=_refund_event("evt_2", "pi_whf", refund_ref="re_pend", status="failed"), raw_payload=b"{}")
    db_session.commit()
    db_session.refresh(refund)
    assert refund.status == RefundStatus.FAILED
    assert payment.status == PaymentStatus.SUCCEEDED


def test_retail_gateway_refund_now_records_refund_row(db_session):
    account = _retail_account(db_session, "whretail")
    invoice, payment = _paid(db_session, account=account, ref="pi_retail")
    PaymentWebhookService.process(db_session, gateway="stripe", event=_refund_event("evt_r", "pi_retail", refund_ref="re_dash"), raw_payload=b"{}")
    db_session.commit()
    db_session.refresh(payment)
    db_session.refresh(invoice)
    refund = db_session.execute(select(Refund).where(Refund.payment_id == payment.id)).scalar_one()
    assert refund.reason == RefundReason.GATEWAY_INITIATED
    assert refund.idempotency_key == "stripe-refund:evt_r"
    assert refund.status == RefundStatus.SUCCEEDED
    assert payment.refunded_cents == 2000
    assert invoice.status == InvoiceStatus.FAILED
    actions = set(db_session.scalars(select(UserAuditEvent.action)))
    assert {"billing.payment_reversed", "billing.refund_succeeded"} <= actions


# -------------------------------------------------------------- reconciler

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def _pending(db, suffix, *, requested=NOW - timedelta(minutes=10), reason=RefundReason.CUSTOMER_REQUEST):
    _invoice, payment = _paid(db, reseller=_reseller(db, suffix))
    refund = _request(db, payment, FakeGateway(outcome=TIMEOUT), key=f"key-{suffix}", reason=reason)
    refund.requested_at = requested
    refund.next_attempt_at = NOW - timedelta(minutes=1)
    db.commit()
    return refund, payment


def test_reconciler_claim_race_one_wins(db_session):
    refund, _payment = _pending(db_session, "race")
    first = RefundService.claim(db_session, refund.id, NOW)
    second = RefundService.claim(db_session, refund.id, NOW)
    assert first is not None and second is None


def test_reconciler_lookup_success_completes(db_session):
    refund, payment = _pending(db_session, "look")
    gateway = FakeGateway(lookup=OK)
    result = RefundReconciler.run(db_session, NOW, registry=_registry(gateway))
    db_session.refresh(refund)
    db_session.refresh(payment)
    assert result["succeeded"] == 1
    assert refund.status == RefundStatus.SUCCEEDED
    assert payment.refunded_cents == 2000
    assert gateway.calls == []
    assert gateway.lookups[0]["idempotency_key"] == "key-look"


def test_reconciler_resends_with_same_key_when_lookup_finds_nothing(db_session):
    refund, _payment = _pending(db_session, "resend")
    gateway = FakeGateway(lookup=None, outcome=OK)
    RefundReconciler.run(db_session, NOW, registry=_registry(gateway))
    db_session.refresh(refund)
    assert refund.status == RefundStatus.SUCCEEDED
    assert gateway.calls[0]["idempotency_key"] == "key-resend"


def test_reconciler_moves_to_review_after_24_hours(db_session):
    refund, _payment = _pending(db_session, "review", requested=NOW - timedelta(hours=25))
    gateway = FakeGateway(lookup=None, outcome=OK)
    result = RefundReconciler.run(db_session, NOW, registry=_registry(gateway))
    db_session.refresh(refund)
    assert result["review"] == 1
    assert refund.status == RefundStatus.REVIEW
    assert gateway.calls == []  # past the gateway's idempotency retention: never re-sent


def test_reconciler_backs_off_while_still_pending(db_session):
    refund, _payment = _pending(db_session, "backoff")
    RefundReconciler.run(db_session, NOW, registry=_registry(FakeGateway(lookup=PENDING)))
    db_session.refresh(refund)
    assert refund.status == RefundStatus.PENDING
    assert refund_module.aware(refund.next_attempt_at) > NOW


def test_reconciler_dry_run_changes_nothing(db_session):
    refund, _payment = _pending(db_session, "dry")
    attempts = refund.attempts
    gateway = FakeGateway(lookup=OK)
    result = RefundReconciler.run(db_session, NOW, dry_run=True, registry=_registry(gateway))
    db_session.refresh(refund)
    assert result["seen"] == 1 and result["planned"][0]["refund_id"] == refund.id
    assert refund.status == RefundStatus.PENDING and refund.attempts == attempts
    assert gateway.lookups == []


# ------------------------------------------------- review 1 regression tests


def test_crash_during_gateway_call_is_picked_up_after_claim_expiry(db_session):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "crash"))
    with pytest.raises(SystemExit):
        _request(db_session, payment, FakeGateway(outcome=SystemExit("worker killed")), key="crash-key")
    db_session.rollback()
    refund = db_session.execute(select(Refund).where(Refund.idempotency_key == "crash-key")).scalar_one()
    assert refund.status == RefundStatus.PENDING and refund.claim_token is not None
    assert refund.next_attempt_at is not None
    now = datetime.now(timezone.utc)
    assert RefundReconciler.due(db_session, now) == []
    later = now + refund_module.CLAIM_TTL + timedelta(seconds=1)
    assert [row.id for row in RefundReconciler.due(db_session, later)] == [refund.id]
    RefundReconciler.run(db_session, later, registry=_registry(FakeGateway(lookup=OK)))
    db_session.refresh(refund)
    assert refund.status == RefundStatus.SUCCEEDED


def test_reconciler_leaves_refund_claimed_by_another_worker(db_session):
    from sqlalchemy import update

    refund, _payment = _pending(db_session, "stolen", requested=NOW - timedelta(hours=25))

    class SlowGateway(FakeGateway):
        def lookup_refund(self, **kwargs):
            # The claim expires during the call and another worker claims the refund.
            db_session.execute(update(Refund).where(Refund.id == refund.id).values(claim_token="other"))
            db_session.commit()
            return PENDING

    result = RefundReconciler.run(db_session, NOW, registry=_registry(SlowGateway()))
    db_session.refresh(refund)
    assert result["skipped"] == 1
    assert refund.status == RefundStatus.PENDING
    assert refund.claim_token == "other"


def test_same_key_committed_after_snapshot_replays_through_unique_constraint(db_session, monkeypatch):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "snap"))
    first = _request(db_session, payment, FakeGateway(outcome=PENDING), key="snap-key")
    db_session.commit()
    original = RefundService._by_key
    # A plain read on a stale snapshot does not see the committed row.
    monkeypatch.setattr(
        RefundService,
        "_by_key",
        staticmethod(lambda db, key, *, lock=False: original(db, key, lock=True) if lock else None),
    )
    monkeypatch.setattr(refund_module, "refundable_cents", lambda db, payment, *, lock=False: int(payment.amount_cents))
    gateway = FakeGateway()
    again = _request(db_session, payment, gateway, key="snap-key")
    assert again.id == first.id
    assert gateway.calls == []


def test_integrity_detectors_see_review_and_mismatch(db_session):
    from app.services.alerting import billing_integrity

    refund, payment = _pending(db_session, "detect", requested=NOW - timedelta(hours=25))
    RefundService.move_to_review(db_session, refund.id, failure_code="no_answer")
    payment.refunded_cents = 5
    db_session.commit()
    stuck = billing_integrity.refund_stuck(db_session, {"pending_after_minutes": 60, "max_rows": 50}, NOW)
    mismatch = billing_integrity.refund_mismatch(db_session, {"max_rows": 50}, NOW)
    assert str(refund.id) in {f.resource_id for f in stuck}
    assert str(payment.id) in {f.resource_id for f in mismatch}


# ----------------------------------------------------------------- gateways


def test_stripe_pending_refund_is_not_confirmed_and_lookup_matches_key():
    from tests.services.test_stripe_gateway_gaps import gateway, stripe_module

    module = stripe_module(refund={"id": "re_1", "status": "pending", "amount": 50})
    result = gateway(module).refund_payment(external_ref="pi_1", amount_cents=50, currency="USD", idempotency_key="idem-1")
    assert result.status == GatewayResultStatus.SUCCEEDED and result.pending is True
    assert module.Refund.calls[0]["metadata"]["rackflow_idempotency_key"] == "idem-1"
    module.Refund.list = lambda **kw: {"data": [{"id": "re_9", "status": "succeeded", "amount": 50, "metadata": {"rackflow_idempotency_key": "idem-1"}}]}
    found = gateway(module).lookup_refund(external_ref="pi_1", idempotency_key="idem-1")
    assert found.external_ref == "re_9" and found.pending is False
    assert gateway(module).lookup_refund(external_ref="pi_1", idempotency_key="other") is None


def test_stripe_refund_events_carry_refund_ref_and_status():
    from tests.services.test_stripe_gateway_gaps import gateway

    gw = gateway()
    charge = {"payment_intent": "pi_1", "currency": "usd", "refunds": {"data": [{"id": "re_7", "status": "pending", "amount": 300}]}}
    event = gw.normalize_webhook_event({"id": "evt", "type": "charge.refunded", "data": {"object": charge}})
    assert (event.refund_ref, event.refund_status, event.amount_cents) == ("re_7", "pending", 300)
    failed = gw.normalize_webhook_event({"id": "evt2", "type": "refund.failed", "data": {"object": {"id": "re_7", "payment_intent": "pi_1", "status": "failed", "amount": 300}}})
    assert failed.action == GatewayWebhookAction.PAYMENT_REVERSED and failed.refund_status == "failed"


def test_paypal_lookup_refund_by_id():
    import httpx

    from tests.services.test_paypal_gateway import _gateway

    def handler(request):
        if request.url.path.endswith("/v1/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        assert request.url.path == "/v2/payments/refunds/R1"
        return httpx.Response(200, json={"id": "R1", "status": "COMPLETED", "amount": {"value": "1.50", "currency_code": "USD"}})

    found = _gateway(handler).lookup_refund(external_ref="CAP", idempotency_key="k", refund_ref="R1")
    assert found.status == GatewayResultStatus.SUCCEEDED and found.pending is False and found.amount_cents == 150
    assert _gateway(handler).lookup_refund(external_ref="CAP", idempotency_key="k") is None


# ------------------------------------------------- review 2 regression tests


def test_stripe_charge_refunded_without_list_reports_running_total_only():
    from tests.services.test_stripe_gateway_gaps import gateway

    charge = {"payment_intent": "pi_1", "currency": "usd", "amount_refunded": 2000}
    event = gateway().normalize_webhook_event({"id": "evt", "type": "charge.refunded", "data": {"object": charge}})
    assert event.amount_cents is None and event.refunded_total_cents == 2000


def test_v2_running_total_event_records_only_the_new_part(db_session, v2):
    account = _retail_account(db_session, "total")
    _invoice, payment = _paid(db_session, account=account, ref="pi_total")
    first = _request(db_session, payment, FakeGateway(), amount=500, key="total-1")
    db_session.commit()
    assert first.status == RefundStatus.SUCCEEDED
    event = NormalizedGatewayWebhookEvent(
        event_id="evt_total", event_type="charge.refunded", action=GatewayWebhookAction.PAYMENT_REVERSED,
        external_ref="pi_total", refund_status="succeeded", refunded_total_cents=1200,
    )
    PaymentWebhookService.process(db_session, gateway="stripe", event=event, raw_payload=b"{}")
    db_session.commit()
    db_session.refresh(payment)
    assert payment.refunded_cents == 1200
    new = db_session.execute(select(Refund).where(Refund.idempotency_key == "stripe-refund:evt_total")).scalar_one()
    assert new.amount_cents == 700


def test_failure_event_without_amount_does_not_fail_an_unrelated_refund(db_session):
    _invoice, payment = _paid(db_session, reseller=_reseller(db_session, "unrel"), ref="pi_unrel")
    ours = _request(db_session, payment, FakeGateway(outcome=TIMEOUT), key="unrel-key")
    db_session.commit()
    assert ours.gateway_refund_ref is None
    event = NormalizedGatewayWebhookEvent(
        event_id="evt_unrel", event_type="PAYMENT.REFUND.FAILED", action=GatewayWebhookAction.PAYMENT_REVERSED,
        external_ref="pi_unrel", refund_ref="R-DASHBOARD", refund_status="failed",
    )
    PaymentWebhookService.process(db_session, gateway="stripe", event=event, raw_payload=b"{}")
    db_session.commit()
    db_session.refresh(ours)
    assert ours.status == RefundStatus.PENDING


def test_stripe_lookup_refund_reads_every_page():
    from tests.services.test_stripe_gateway_gaps import gateway, stripe_module

    module = stripe_module()
    pages = {
        None: {"data": [{"id": f"re_{i}", "status": "succeeded", "metadata": {}} for i in range(100)], "has_more": True},
        "re_99": {"data": [{"id": "re_x", "status": "succeeded", "amount": 5, "metadata": {"rackflow_idempotency_key": "late"}}], "has_more": False},
    }
    module.Refund.list = lambda **kw: pages[kw.get("starting_after")]
    found = gateway(module).lookup_refund(external_ref="pi_1", idempotency_key="late")
    assert found is not None and found.external_ref == "re_x"


def test_paypal_lookup_refund_encodes_the_refund_id():
    import httpx

    from tests.services.test_paypal_gateway import _gateway

    paths = []

    def handler(request):
        if request.url.path.endswith("/v1/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        paths.append(request.url.raw_path)
        return httpx.Response(404, json={})

    assert _gateway(handler).lookup_refund(external_ref="CAP", idempotency_key="k", refund_ref="../captures/X?y=1") is None
    assert paths == [b"/v2/payments/refunds/..%2Fcaptures%2FX%3Fy%3D1"]
