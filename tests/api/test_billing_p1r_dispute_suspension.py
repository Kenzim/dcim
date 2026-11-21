"""P1-R: dispute suspension through the real async webhook routes.

The Stripe and PayPal routes are ``async def``. Suspension runs after the
dispute's transaction has committed, so no billing lock is held during
lifecycle calls, and it works from both the async routes and sync callers.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import event as sa_event

from app.models.commerce_audit import UserAuditEvent
from app.models.refund import Refund, RefundReason, RefundStatus
from app.models.reseller import (
    GatewayWebhookEvent,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.service import Service, ServiceStatus, ServiceType
from app.services.service_lifecycle import ServiceLifecycle, ServiceLifecycleError
from app.services.payments.base import GatewayWebhookAction, NormalizedGatewayWebhookEvent
from app.services.payments.webhooks import PaymentWebhookService
from tests.api.test_reseller_panel import _login, fake_stripe  # noqa: F401
from tests.services.test_billing_w0e_review_fixes import lock_calls  # noqa: F401

_NUMBER = [970000]


def _disputable(db, reseller, ref):
    _NUMBER[0] += 1
    service = Service(name=f"proxy-{ref}", service_type=ServiceType.HTTP_PROXY, status=ServiceStatus.ACTIVE)
    db.add(service)
    db.flush()
    invoice = Invoice(
        invoice_number=_NUMBER[0],
        reseller_id=reseller.id,
        service_id=service.id,
        purpose=InvoicePurpose.DEPLOY_CHARGE,
        status=InvoiceStatus.PAID,
        amount_cents=1500,
        currency="USD",
    )
    db.add(invoice)
    db.flush()
    db.add(
        Payment(
            invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED,
            amount_cents=1500, currency="USD", external_ref=ref,
        )
    )
    db.commit()
    return service, invoice


def _dispute_event(event_id, ref):
    return {
        "id": event_id,
        "type": "charge.dispute.created",
        "data": {"object": {"id": f"dp_{event_id}", "payment_intent": ref, "amount": 1500, "currency": "usd"}},
    }


def _committed(db_session, *, gateway, event_id, payment_ref, service_id):
    """Read back what the webhook committed.

    The test engine shares one SQLite connection between sessions, so the
    request's session is rolled back first: whatever is still there was
    committed. It is then read in a fresh session.
    """
    from tests.conftest import TestingSessionLocal

    db_session.rollback()
    fresh = TestingSessionLocal()
    try:
        payment = fresh.query(Payment).filter(Payment.external_ref == payment_ref).one()
        return SimpleNamespace(
            webhook_event=fresh.query(GatewayWebhookEvent)
            .filter(GatewayWebhookEvent.gateway == gateway, GatewayWebhookEvent.event_id == event_id)
            .one_or_none(),
            refunds=[
                (r.reason, r.status, int(r.amount_cents), r.idempotency_key)
                for r in fresh.query(Refund).filter(Refund.payment_id == payment.id)
            ],
            payment=(payment.status, int(payment.refunded_cents or 0)),
            invoice_status=fresh.get(Invoice, payment.invoice_id).status,
            service_status=fresh.get(Service, service_id).status,
            suspend_failed_audits=[
                event.event_metadata
                for event in fresh.query(UserAuditEvent).filter(
                    UserAuditEvent.action == "billing.dispute_suspend_failed",
                    UserAuditEvent.resource_id == str(service_id),
                )
            ],
        )
    finally:
        fresh.close()


def _post_stripe(client):
    return client.post("/api/reseller-panel/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "valid"})


def test_stripe_dispute_through_async_route_suspends_linked_service(client, db_session, fake_stripe):  # noqa: F811
    reseller, _headers = _login(client, db_session, "p1r-dispute")
    service, _invoice = _disputable(db_session, reseller, "pi_p1r_dispute")
    fake_stripe.event = _dispute_event("evt_p1r_dispute", "pi_p1r_dispute")

    response = _post_stripe(client)

    # 1. The route answers 200 with its usual body.
    assert response.status_code == 200, response.text
    assert response.json() == {"received": True, "duplicate": False}
    state = _committed(db_session, gateway="stripe", event_id="evt_p1r_dispute", payment_ref="pi_p1r_dispute", service_id=service.id)
    # 2. The dispute and its refund row are committed.
    assert state.webhook_event is not None
    assert state.refunds == [(RefundReason.DISPUTE, RefundStatus.SUCCEEDED, 1500, "stripe-dispute:evt_p1r_dispute")]
    assert state.payment == (PaymentStatus.REFUNDED, 1500)
    assert state.invoice_status == InvoiceStatus.FAILED
    # 3. The linked service is suspended.
    assert state.service_status == ServiceStatus.SUSPENDED
    assert state.suspend_failed_audits == []
    # A gateway retry is a duplicate and changes nothing.
    assert _post_stripe(client).json() == {"received": True, "duplicate": True}


@pytest.mark.parametrize("error", [ServiceLifecycleError("hypervisor unreachable"), RuntimeError("plugin crashed")])
def test_stripe_dispute_with_failing_suspension_is_still_committed(client, db_session, fake_stripe, monkeypatch, caplog, error):  # noqa: F811
    reseller, _headers = _login(client, db_session, f"p1r-dispute-fail-{type(error).__name__}")
    ref = f"pi_p1r_fail_{type(error).__name__}"
    service, _invoice = _disputable(db_session, reseller, ref)
    fake_stripe.event = _dispute_event(f"evt_{ref}", ref)

    async def failing_suspend(self, db, svc, *, reason):
        svc.status = ServiceStatus.SUSPENDED  # a half-done change must be rolled back
        db.flush()
        raise error

    monkeypatch.setattr(ServiceLifecycle, "suspend", failing_suspend)
    with caplog.at_level(logging.ERROR, logger="app.services.payments.webhooks"):
        response = _post_stripe(client)

    assert response.status_code == 200, response.text
    assert response.json() == {"received": True, "duplicate": False}
    state = _committed(db_session, gateway="stripe", event_id=f"evt_{ref}", payment_ref=ref, service_id=service.id)
    assert state.webhook_event is not None
    assert state.refunds == [(RefundReason.DISPUTE, RefundStatus.SUCCEEDED, 1500, f"stripe-dispute:evt_{ref}")]
    assert state.payment == (PaymentStatus.REFUNDED, 1500)
    assert state.service_status == ServiceStatus.ACTIVE
    assert [audit["reason"] for audit in state.suspend_failed_audits] == ["dispute"]
    assert str(error) in state.suspend_failed_audits[0]["error"]
    assert any(f"Dispute suspend failed for service {service.id}" in r.getMessage() for r in caplog.records)


def test_sync_caller_dispute_suspends_after_commit_with_no_lock_held(db_session, client, lock_calls):  # noqa: F811
    reseller, _headers = _login(client, db_session, "p1r-dispute-sync")
    service, _invoice = _disputable(db_session, reseller, "pi_p1r_sync")
    seen = {}
    lock_calls.clear()
    marker = [0]
    commits = [0]

    async def recording_suspend(self, db, svc, *, reason):
        # The dispute's transaction has committed (its locks were taken and
        # released) and no billing lock has been taken since.
        seen["uncommitted_writes"] = db.connection().connection.dbapi_connection.in_transaction
        seen["refund_recorded"] = db.query(Refund).filter(Refund.idempotency_key == "stripe-dispute:evt_p1r_sync").count() == 1
        seen["dispute_took_locks"] = marker[0] > 0
        seen["locks_since_commit"] = list(lock_calls[marker[0]:])
        svc.status = ServiceStatus.SUSPENDED
        db.flush()
        return True

    def on_commit(_session):
        commits[0] += 1
        marker[0] = len(lock_calls)

    sa_event.listen(db_session, "after_commit", on_commit)
    try:
        with pytest.MonkeyPatch.context() as patch:
            from app.services.service_lifecycle import ServiceLifecycle

            patch.setattr(ServiceLifecycle, "suspend", recording_suspend)
            PaymentWebhookService.process(
                db_session,
                gateway="stripe",
                event=NormalizedGatewayWebhookEvent(
                    event_id="evt_p1r_sync", event_type="charge.dispute.created",
                    action=GatewayWebhookAction.PAYMENT_DISPUTED, external_ref="pi_p1r_sync",
                ),
                raw_payload=b"{}",
            )
            db_session.commit()
    finally:
        sa_event.remove(db_session, "after_commit", on_commit)
    db_session.expire_all()
    assert db_session.get(Service, service.id).status == ServiceStatus.SUSPENDED
    assert seen == {
        "uncommitted_writes": False,
        "refund_recorded": True,
        "dispute_took_locks": True,
        "locks_since_commit": [],
    }


def test_failed_dispute_transaction_does_not_suspend(db_session, client, monkeypatch):
    reseller, _headers = _login(client, db_session, "p1r-dispute-rollback")
    service, _invoice = _disputable(db_session, reseller, "pi_p1r_rb")
    from app.services import refund_service

    def explode(*_args, **_kwargs):
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(refund_service.RefundService, "record_gateway_refund", staticmethod(explode))
    with pytest.raises(RuntimeError):
        PaymentWebhookService.process(
            db_session,
            gateway="stripe",
            event=NormalizedGatewayWebhookEvent(
                event_id="evt_p1r_rb", event_type="charge.dispute.created",
                action=GatewayWebhookAction.PAYMENT_DISPUTED, external_ref="pi_p1r_rb",
            ),
            raw_payload=b"{}",
        )
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(Service, service.id).status == ServiceStatus.ACTIVE
    assert not db_session.info.get(PaymentWebhookService.DEFERRED_SUSPENSION_KEY)


class _FakePayPal:
    name = "paypal"

    def __init__(self, normalized):
        self.normalized = normalized

    def construct_webhook_event(self, *, payload, headers):
        return {"id": self.normalized.event_id}

    def normalize_webhook_event(self, _event):
        return self.normalized


def test_paypal_dispute_through_async_route_suspends_linked_service(client, db_session):
    from app.services.payments.registry import payment_gateway_registry

    reseller, _headers = _login(client, db_session, "p1r-dispute-pp")
    service, _invoice = _disputable(db_session, reseller, "CAP-P1R")
    db_session.get(Payment, db_session.query(Payment.id).filter(Payment.external_ref == "CAP-P1R").scalar()).gateway = "paypal"
    db_session.commit()
    previous = payment_gateway_registry.maybe_get("paypal")
    payment_gateway_registry.register(
        _FakePayPal(
            NormalizedGatewayWebhookEvent(
                event_id="WH-P1R", event_type="CUSTOMER.DISPUTE.CREATED",
                action=GatewayWebhookAction.PAYMENT_DISPUTED, external_ref="CAP-P1R",
            )
        )
    )
    try:
        response = client.post("/api/reseller-panel/webhooks/paypal", content=b"{}")
    finally:
        if previous is not None:
            payment_gateway_registry.register(previous)
        else:
            payment_gateway_registry.unregister("paypal")
    assert response.status_code == 200, response.text
    assert response.json() == {"received": True, "duplicate": False}
    state = _committed(db_session, gateway="paypal", event_id="WH-P1R", payment_ref="CAP-P1R", service_id=service.id)
    assert state.webhook_event is not None
    assert state.refunds == [(RefundReason.DISPUTE, RefundStatus.SUCCEEDED, 1500, "paypal-dispute:WH-P1R")]
    assert state.payment == (PaymentStatus.REFUNDED, 1500)
    assert state.service_status == ServiceStatus.SUSPENDED
    assert state.suspend_failed_audits == []


def test_stripe_dispute_two_services_first_fails_second_still_suspended(client, db_session, fake_stripe, monkeypatch, caplog):  # noqa: F811
    reseller, _headers = _login(client, db_session, "p1r-dispute-two")
    first, _invoice = _disputable(db_session, reseller, "pi_p1r_two")
    second = Service(name="proxy-pi_p1r_two-b", service_type=ServiceType.HTTP_PROXY, status=ServiceStatus.ACTIVE)
    db_session.add(second)
    db_session.commit()
    first_id, second_id = first.id, second.id
    # The invoice links both services (as an order with two items would).
    monkeypatch.setattr(PaymentWebhookService, "_linked_service_ids", staticmethod(lambda db, invoice: [first_id, second_id]))
    original = ServiceLifecycle.suspend

    async def first_fails(self, db, svc, *, reason):
        if svc.id == first_id:
            raise ServiceLifecycleError("hypervisor unreachable")
        return await original(self, db, svc, reason=reason)

    monkeypatch.setattr(ServiceLifecycle, "suspend", first_fails)
    fake_stripe.event = _dispute_event("evt_p1r_two", "pi_p1r_two")
    with caplog.at_level(logging.ERROR, logger="app.services.payments.webhooks"):
        response = _post_stripe(client)

    assert response.status_code == 200, response.text
    assert response.json() == {"received": True, "duplicate": False}
    state = _committed(db_session, gateway="stripe", event_id="evt_p1r_two", payment_ref="pi_p1r_two", service_id=first_id)
    assert state.refunds == [(RefundReason.DISPUTE, RefundStatus.SUCCEEDED, 1500, "stripe-dispute:evt_p1r_two")]
    assert state.payment == (PaymentStatus.REFUNDED, 1500)
    assert state.invoice_status == InvoiceStatus.FAILED
    assert state.service_status == ServiceStatus.ACTIVE
    assert len(state.suspend_failed_audits) == 1
    assert "hypervisor unreachable" in state.suspend_failed_audits[0]["error"]
    second_state = _committed(db_session, gateway="stripe", event_id="evt_p1r_two", payment_ref="pi_p1r_two", service_id=second_id)
    assert second_state.service_status == ServiceStatus.SUSPENDED
    assert second_state.suspend_failed_audits == []
    assert any(f"Dispute suspend failed for service {first_id}" in r.getMessage() for r in caplog.records)
