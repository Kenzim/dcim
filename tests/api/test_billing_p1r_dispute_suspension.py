"""P1-R: dispute suspension through the real async webhook routes.

The Stripe and PayPal routes are ``async def``. Suspension runs after the
dispute's transaction has committed, so no billing lock is held during
lifecycle calls, and it works from both the async routes and sync callers.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event as sa_event

from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.models.service import Service, ServiceStatus, ServiceType
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


def test_stripe_dispute_through_async_route_suspends_linked_service(client, db_session, fake_stripe):  # noqa: F811
    reseller, _headers = _login(client, db_session, "p1r-dispute")
    service, invoice = _disputable(db_session, reseller, "pi_p1r_dispute")
    fake_stripe.event = _dispute_event("evt_p1r_dispute", "pi_p1r_dispute")

    response = client.post("/api/reseller-panel/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "valid"})

    assert response.status_code == 200, response.text
    assert response.json() == {"received": True, "duplicate": False}
    db_session.expire_all()
    assert db_session.get(Service, service.id).status == ServiceStatus.SUSPENDED
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.FAILED
    replay = client.post("/api/reseller-panel/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "valid"})
    assert replay.json() == {"received": True, "duplicate": True}


def test_sync_caller_dispute_suspends_after_commit_with_no_lock_held(db_session, client, lock_calls):  # noqa: F811
    reseller, _headers = _login(client, db_session, "p1r-dispute-sync")
    service, _invoice = _disputable(db_session, reseller, "pi_p1r_sync")
    seen = {}

    async def recording_suspend(self, db, svc, *, reason):
        # The dispute's transaction is already committed: no locks are held.
        seen["in_transaction"] = db.in_transaction() and bool(db.dirty or db.new)
        seen["locks_since_commit"] = list(lock_calls[marker[0]:])
        svc.status = ServiceStatus.SUSPENDED
        db.flush()
        return True

    marker = [0]

    def on_commit(_session):
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
    assert seen == {"in_transaction": False, "locks_since_commit": []}


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
    db_session.expire_all()
    assert db_session.get(Service, service.id).status == ServiceStatus.SUSPENDED
