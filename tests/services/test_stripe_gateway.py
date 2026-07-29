from types import SimpleNamespace

from app.services.payments.base import GatewayResultStatus
from app.services.payments.stripe_gateway import StripeGateway


class _StripeError(Exception):
    pass


class _Resource:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _PaymentMethodResource:
    def retrieve(self, method_ref, **kwargs):
        return {
            "id": method_ref,
            "customer": "cus_1",
            "type": "card",
            "card": {"brand": "visa", "last4": "4242"},
        }

    def detach(self, method_ref, **kwargs):
        return {"id": method_ref}


def _stripe_module(intent):
    module = SimpleNamespace()
    module.Customer = _Resource({"id": "cus_1"})
    module.SetupIntent = _Resource(
        {"id": "seti_1", "client_secret": "seti_secret"}
    )
    module.PaymentIntent = _Resource(intent)
    module.PaymentMethod = _PaymentMethodResource()
    module.Webhook = SimpleNamespace(
        construct_event=lambda payload, signature, secret: {
            "id": "evt_1",
            "type": "test",
        }
    )
    module.error = SimpleNamespace(
        CardError=type("CardError", (_StripeError,), {}),
        APIConnectionError=type("APIConnectionError", (_StripeError,), {}),
        APIError=type("APIError", (_StripeError,), {}),
        RateLimitError=type("RateLimitError", (_StripeError,), {}),
        StripeError=_StripeError,
    )
    return module


def test_stripe_uses_integer_lowercase_off_session_intent():
    module = _stripe_module({"id": "pi_1", "status": "succeeded"})
    gateway = StripeGateway(
        secret_key="sk_test_mock",
        webhook_secret="whsec_mock",
        stripe_module=module,
    )

    result = gateway.charge_off_session(
        customer_ref="cus_1",
        method_ref="pm_1",
        amount_cents=1234,
        currency="USD",
        idempotency_key="invoice-1",
        metadata={"rackflow_invoice_id": "1"},
    )

    assert result.status == GatewayResultStatus.SUCCEEDED
    call = module.PaymentIntent.calls[0]
    assert call["amount"] == 1234
    assert type(call["amount"]) is int
    assert call["currency"] == "usd"
    assert call["off_session"] is True
    assert call["confirm"] is True
    assert call["idempotency_key"] == "invoice-1"


def test_stripe_setup_customer_method_and_action_mapping():
    module = _stripe_module(
        {
            "id": "pi_action",
            "status": "requires_action",
            "client_secret": "pi_action_secret",
        }
    )
    gateway = StripeGateway(
        secret_key="sk_test_mock",
        webhook_secret="whsec_mock",
        stripe_module=module,
    )

    customer = gateway.create_customer(
        reseller_id=7, email="reseller@example.com"
    )
    setup = gateway.create_setup_intent(customer_ref=customer.external_ref)
    method = gateway.get_payment_method(method_ref="pm_1")
    result = gateway.charge_off_session(
        customer_ref="cus_1",
        method_ref="pm_1",
        amount_cents=500,
        currency="USD",
        idempotency_key="invoice-2",
        metadata={},
    )

    assert customer.external_ref == "cus_1"
    assert setup.client_secret == "seti_secret"
    assert method.label == "Visa •••• 4242"
    assert result.status == GatewayResultStatus.REQUIRES_ACTION
    assert result.client_secret == "pi_action_secret"
