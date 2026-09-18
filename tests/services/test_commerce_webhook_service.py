"""Unit tests for commerce webhook enqueue and delivery."""
from __future__ import annotations

from unittest.mock import Mock, patch

from app.models.commerce_webhook import WebhookDeliveryStatus
from app.services.commerce_webhook_service import CommerceWebhookService


def test_endpoint_wants_and_sign_payload():
    assert CommerceWebhookService._endpoint_wants(Mock(events=[]), "order.paid") is True
    assert CommerceWebhookService._endpoint_wants(Mock(events=["order.paid"]), "order.paid") is True
    assert CommerceWebhookService._endpoint_wants(Mock(events=["order.paid"]), "ticket.created") is False
    sig = CommerceWebhookService.sign_payload("secret", b"{}")
    assert len(sig) == 64


def test_enqueue_filters_and_deliver_pending(db_session):
    assert CommerceWebhookService.enqueue(db_session, "unknown.event", {}) == []

    wanted = CommerceWebhookService.create_endpoint(
        db_session, url="https://hooks.example/a", secret="sec-a", events=["order.paid"]
    )
    skipped = CommerceWebhookService.create_endpoint(
        db_session, url="https://hooks.example/b", secret="sec-b", events=["ticket.created"]
    )
    serialized = CommerceWebhookService.serialize_endpoint(wanted)
    assert serialized["url"] == "https://hooks.example/a"
    assert skipped.id != wanted.id

    delivered = CommerceWebhookService.enqueue(db_session, "order.paid", {"id": 1})
    assert len(delivered) == 1
    assert delivered[0].endpoint_id == wanted.id

    ok = Mock(status_code=204, text="")
    with patch("app.services.commerce_webhook_service.httpx.post", return_value=ok):
        assert CommerceWebhookService.deliver_pending(db_session) == 1
    db_session.refresh(delivered[0])
    assert delivered[0].status == WebhookDeliveryStatus.DELIVERED

    fail_rows = CommerceWebhookService.enqueue(db_session, "order.paid", {"id": 2})
    bad = Mock(status_code=500, text="boom")
    with patch("app.services.commerce_webhook_service.httpx.post", return_value=bad):
        assert CommerceWebhookService.deliver_pending(db_session) == 0
    db_session.refresh(fail_rows[0])
    assert fail_rows[0].status == WebhookDeliveryStatus.FAILED

    err_rows = CommerceWebhookService.enqueue(db_session, "order.paid", {"id": 3})
    with patch(
        "app.services.commerce_webhook_service.httpx.post",
        side_effect=RuntimeError("timeout"),
    ):
        assert CommerceWebhookService.deliver_pending(db_session) == 0
    db_session.refresh(err_rows[0])
    assert "timeout" in (err_rows[0].last_error or "")

    wanted.enabled = False
    db_session.flush()
    disabled_rows = CommerceWebhookService.enqueue(db_session, "order.paid", {"id": 4})
    assert disabled_rows == []
    # Re-enable enqueue via empty events, then disable before deliver.
    catch_all = CommerceWebhookService.create_endpoint(
        db_session, url="https://hooks.example/c", secret="sec-c", events=[], enabled=True
    )
    pending = CommerceWebhookService.enqueue(db_session, "invoice.paid", {"id": 5})
    assert pending
    catch_all.enabled = False
    db_session.flush()
    CommerceWebhookService.deliver_pending(db_session)
    db_session.refresh(pending[0])
    assert pending[0].status == WebhookDeliveryStatus.FAILED
