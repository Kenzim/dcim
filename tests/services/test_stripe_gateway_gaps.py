from types import SimpleNamespace

import pytest

from app.services.payments.base import (
    GatewayNotConfiguredError, GatewayResultStatus, GatewayWebhookAction,
    PaymentGatewayError,
)
from app.services.payments.stripe_gateway import StripeGateway, _value


class StripeFailure(Exception):
    pass


class Resource:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or {}, error, []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def stripe_module(intent=None, refund=None):
    errors = SimpleNamespace(
        CardError=type("CardError", (StripeFailure,), {}),
        APIConnectionError=type("APIConnectionError", (StripeFailure,), {}),
        APIError=type("APIError", (StripeFailure,), {}),
        RateLimitError=type("RateLimitError", (StripeFailure,), {}),
        InvalidRequestError=type("InvalidRequestError", (StripeFailure,), {}),
        StripeError=StripeFailure,
    )
    return SimpleNamespace(
        Customer=Resource({"id": "cus_1"}), SetupIntent=Resource({"id": "seti", "client_secret": "secret"}),
        PaymentIntent=Resource(intent or {"id": "pi", "status": "succeeded"}),
        Refund=Resource(refund or {"id": "re", "status": "succeeded"}),
        PaymentMethod=SimpleNamespace(list=lambda **kwargs: {"data": []}, detach=lambda *args, **kwargs: None),
        Webhook=SimpleNamespace(construct_event=lambda *args: {"id": "event"}),
        error=errors,
    )


def gateway(module=None, webhook_secret="whsec"):
    return StripeGateway(secret_key="sk_test", webhook_secret=webhook_secret, stripe_module=module or stripe_module())


@pytest.mark.parametrize("obj, name, default, expected", [
    ({"x": 1}, "x", None, 1), ({}, "x", "d", "d"),
    (SimpleNamespace(x=1), "x", None, 1), (SimpleNamespace(), "x", "d", "d"),
])
def test_value_handles_mapping_and_object_attributes(obj, name, default, expected):
    assert _value(obj, name, default) == expected


@pytest.mark.parametrize("method, expected", [
    ({"id": "pm_1", "customer": {"id": "cus_1"}, "type": "card", "card": {"brand": "visa", "last4": "4242"}}, ("Visa •••• 4242", "cus_1")),
    ({"id": "pm_2", "customer": None, "type": "bank_account", "card": {}}, ("Bank_Account", "")),
    (SimpleNamespace(id="pm_3", customer="cus_3", type="card", card=SimpleNamespace(brand="mc", last4="0001")), ("Mc •••• 0001", "cus_3")),
])
def test_payment_method_normalization_handles_shapes(method, expected):
    result = StripeGateway._payment_method_result(method)
    assert (result.label, result.customer_ref) == expected


@pytest.mark.parametrize("intent, expected", [
    ({"id": "p1", "status": "succeeded"}, GatewayResultStatus.SUCCEEDED),
    ({"id": "p2", "status": "requires_action", "client_secret": "x"}, GatewayResultStatus.REQUIRES_ACTION),
    ({"id": "p3", "status": "requires_source_action", "client_secret": "x"}, GatewayResultStatus.REQUIRES_ACTION),
    ({"id": "p4", "status": "processing"}, GatewayResultStatus.TRANSIENT_ERROR),
    ({"id": "p5", "status": "requires_confirmation"}, GatewayResultStatus.TRANSIENT_ERROR),
    ({"id": "p6", "status": "canceled", "last_payment_error": {"code": "gone"}}, GatewayResultStatus.DECLINED),
])
def test_intent_result_maps_stripe_statuses(intent, expected):
    assert StripeGateway._intent_result(intent).status == expected


@pytest.mark.parametrize("amount, currency", [(0, "USD"), (-1, "USD"), (1.5, "USD"), (1, "EUR"), (1, "GBP")])
def test_charge_rejects_invalid_amount_or_currency(amount, currency):
    with pytest.raises(PaymentGatewayError):
        gateway().charge_off_session(customer_ref="cus", method_ref="pm", amount_cents=amount, currency=currency, idempotency_key="i", metadata={})


@pytest.mark.parametrize("status, expected", [("succeeded", GatewayResultStatus.SUCCEEDED), ("pending", GatewayResultStatus.SUCCEEDED), ("failed", GatewayResultStatus.DECLINED)])
def test_refund_maps_success_pending_and_failure(status, expected):
    result = gateway(stripe_module(refund={"id": "re_1", "status": status})).refund_payment(external_ref="pi_1", amount_cents=50, currency="USD", idempotency_key="i")
    assert result.status == expected


@pytest.mark.parametrize("event_type, obj, action", [
    ("payment_intent.succeeded", {"id": "pi", "amount_received": 123, "currency": "usd"}, GatewayWebhookAction.PAYMENT_COMPLETED),
    ("payment_intent.payment_failed", {"id": "pi", "last_payment_error": {"code": "declined"}}, GatewayWebhookAction.PAYMENT_FAILED),
    ("charge.refunded", {"payment_intent": {"id": "pi"}}, GatewayWebhookAction.PAYMENT_REVERSED),
    ("refund.updated", {"payment_intent": "pi", "status": "succeeded"}, GatewayWebhookAction.PAYMENT_REVERSED),
    ("charge.dispute.created", {"id": "ch", "payment_intent": "pi"}, GatewayWebhookAction.PAYMENT_DISPUTED),
    ("customer.created", {}, GatewayWebhookAction.IGNORED),
])
def test_normalize_webhook_event_maps_supported_events(event_type, obj, action):
    event = {"id": "evt", "type": event_type, "data": {"object": obj}}
    result = gateway().normalize_webhook_event(event)
    assert result.action == action


def test_gateway_requires_secret_and_valid_webhook_signature():
    with pytest.raises(GatewayNotConfiguredError):
        StripeGateway(secret_key="", stripe_module=stripe_module())
    with pytest.raises(PaymentGatewayError, match="signature"):
        gateway().construct_webhook_event(payload=b"{}")
    assert gateway().construct_webhook_event(payload=b"{}", headers={"Stripe-Signature": "sig"}) == {"id": "event"}
