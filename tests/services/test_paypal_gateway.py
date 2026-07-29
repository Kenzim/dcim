import json

import httpx
import pytest

from app.services.payments.base import (
    GatewayResultStatus,
    GatewayWebhookAction,
    PaymentGatewayError,
)
from app.services.payments.paypal_gateway import PayPalGateway


def _response(request, status_code, payload=None):
    return httpx.Response(status_code, json=payload, request=request)


def _gateway(handler):
    return PayPalGateway(
        client_id="paypal-client",
        client_secret="paypal-secret",
        webhook_id="WH-1",
        environment="sandbox",
        return_base_url="https://billing.example.test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_oauth_cache_and_vault_setup_token_exchange():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/oauth2/token":
            assert request.headers["authorization"].startswith("Basic ")
            return _response(
                request,
                200,
                {"access_token": "access-token", "expires_in": 3600},
            )
        if request.url.path == "/v3/vault/setup-tokens":
            body = json.loads(request.content)
            assert body["customer"] == {
                "merchant_customer_id": "rackflow-reseller-7"
            }
            assert (
                body["payment_source"]["paypal"]["experience_context"][
                    "return_url"
                ]
                == "https://billing.example.test/reseller/payments/paypal/return"
            )
            return _response(
                request,
                201,
                {
                    "id": "SETUP-1",
                    "customer": {"id": "customer_1"},
                    "status": "PAYER_ACTION_REQUIRED",
                    "links": [
                        {
                            "rel": "approve",
                            "href": "https://paypal.test/approve/SETUP-1",
                        }
                    ],
                },
            )
        if (
            request.method == "POST"
            and request.url.path == "/v3/vault/payment-tokens"
        ):
            assert json.loads(request.content)["payment_source"]["token"] == {
                "id": "SETUP-1",
                "type": "SETUP_TOKEN",
            }
            return _response(
                request,
                201,
                {
                    "id": "VAULT-1",
                    "customer": {
                        "id": "customer_1",
                        "merchant_customer_id": "rackflow-reseller-7",
                    },
                    "payment_source": {
                        "paypal": {"email_address": "payer@example.test"}
                    },
                },
            )
        if request.url.path == "/v3/vault/payment-tokens/VAULT-1":
            return _response(
                request,
                200,
                {
                    "id": "VAULT-1",
                    "customer": {
                        "id": "customer_1",
                        "merchant_customer_id": "rackflow-reseller-7",
                    },
                    "payment_source": {
                        "paypal": {"email_address": "payer@example.test"}
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    gateway = _gateway(handler)
    setup = gateway.create_vault_setup(reseller_id=7)
    exchanged = gateway.complete_vault_setup(setup_token_ref=setup.external_ref)
    retrieved = gateway.get_payment_method(method_ref=exchanged.external_ref)

    assert setup.approval_url == "https://paypal.test/approve/SETUP-1"
    assert retrieved.external_ref == "VAULT-1"
    assert retrieved.customer_ref == "customer_1"
    assert retrieved.label == "PayPal (payer@example.test)"
    assert retrieved.metadata == {
        "merchant_customer_id": "rackflow-reseller-7",
        "payer_email": "payer@example.test",
    }
    assert sum(call.url.path == "/v1/oauth2/token" for call in calls) == 1


def test_off_session_order_captures_and_returns_capture_reference():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return _response(
                request, 200, {"access_token": "token", "expires_in": 3600}
            )
        if request.url.path == "/v2/checkout/orders":
            body = json.loads(request.content)
            paypal = body["payment_source"]["paypal"]
            assert paypal["vault_id"] == "VAULT-1"
            assert paypal["stored_credential"] == {
                "payment_initiator": "MERCHANT",
                "usage": "SUBSEQUENT",
                "usage_pattern": "UNSCHEDULED_POSTPAID",
            }
            assert body["purchase_units"][0]["custom_id"] == "42"
            return _response(
                request,
                201,
                {"id": "ORDER-1", "status": "APPROVED"},
            )
        if request.url.path == "/v2/checkout/orders/ORDER-1/capture":
            return _response(
                request,
                201,
                {
                    "id": "ORDER-1",
                    "status": "COMPLETED",
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {
                                        "id": "CAPTURE-1",
                                        "status": "COMPLETED",
                                        "amount": {
                                            "currency_code": "USD",
                                            "value": "12.34",
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    result = _gateway(handler).charge_off_session(
        customer_ref="customer_1",
        method_ref="VAULT-1",
        amount_cents=1234,
        currency="USD",
        idempotency_key="invoice-42-paypal",
        metadata={"rackflow_invoice_id": "42"},
    )

    assert result.status == GatewayResultStatus.SUCCEEDED
    assert result.external_ref == "CAPTURE-1"
    request_ids = [
        request.headers["paypal-request-id"]
        for request in requests
        if request.url.path.startswith("/v2/checkout/orders")
    ]
    assert len(request_ids) == 2
    assert request_ids[0] != request_ids[1]


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (
            200,
            {
                "id": "ORDER-ACTION",
                "status": "PAYER_ACTION_REQUIRED",
                "links": [
                    {
                        "rel": "payer-action",
                        "href": "https://paypal.test/action",
                    }
                ],
            },
            GatewayResultStatus.REQUIRES_ACTION,
        ),
        (
            422,
            {
                "name": "UNPROCESSABLE_ENTITY",
                "details": [{"issue": "INSTRUMENT_DECLINED"}],
            },
            GatewayResultStatus.DECLINED,
        ),
    ],
)
def test_payer_action_and_decline_mapping(status_code, payload, expected):
    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return _response(
                request, 200, {"access_token": "token", "expires_in": 3600}
            )
        return _response(request, status_code, payload)

    result = _gateway(handler).charge_off_session(
        customer_ref=None,
        method_ref="VAULT-1",
        amount_cents=500,
        currency="USD",
        idempotency_key="invoice-1",
        metadata={},
    )

    assert result.status == expected
    if expected == GatewayResultStatus.REQUIRES_ACTION:
        assert result.client_secret == "https://paypal.test/action"


def test_webhook_postback_verification_and_normalization():
    verification_status = "SUCCESS"
    posted = []

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return _response(
                request, 200, {"access_token": "token", "expires_in": 3600}
            )
        posted.append(json.loads(request.content))
        return _response(
            request, 200, {"verification_status": verification_status}
        )

    gateway = _gateway(handler)
    event = {
        "id": "WH-EVENT-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAPTURE-1",
            "amount": {"currency_code": "USD", "value": "20.00"},
            "supplementary_data": {
                "related_ids": {"order_id": "ORDER-1"}
            },
        },
    }
    headers = {
        "PayPal-Auth-Algo": "SHA256withRSA",
        "PayPal-Cert-Url": "https://api.paypal.com/cert.pem",
        "PayPal-Transmission-Id": "transmission-1",
        "PayPal-Transmission-Sig": "signature",
        "PayPal-Transmission-Time": "2026-07-28T18:00:00Z",
    }
    event_payload = json.dumps(event).encode()

    verified = gateway.construct_webhook_event(
        payload=event_payload, headers=headers
    )
    normalized = gateway.normalize_webhook_event(verified)

    assert posted[0]["webhook_id"] == "WH-1"
    assert posted[0]["webhook_event"] == event
    assert normalized.action == GatewayWebhookAction.PAYMENT_COMPLETED
    assert normalized.external_ref == "CAPTURE-1"
    assert normalized.related_external_ref == "ORDER-1"
    assert normalized.amount_cents == 2000

    with pytest.raises(PaymentGatewayError):
        gateway.construct_webhook_event(payload=event_payload, headers={})

    verification_status = "FAILURE"
    with pytest.raises(PaymentGatewayError):
        gateway.construct_webhook_event(
            payload=event_payload, headers=headers
        )


def test_refund_and_dispute_use_original_capture_reference():
    gateway = _gateway(
        lambda request: _response(
            request, 200, {"access_token": "unused", "expires_in": 3600}
        )
    )
    refunded = gateway.normalize_webhook_event(
        {
            "id": "WH-REFUND",
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {
                "id": "REFUND-1",
                "supplementary_data": {
                    "related_ids": {"capture_id": "CAPTURE-1"}
                },
            },
        }
    )
    disputed = gateway.normalize_webhook_event(
        {
            "id": "WH-DISPUTE",
            "event_type": "CUSTOMER.DISPUTE.CREATED",
            "resource": {
                "disputed_transactions": [
                    {"seller_transaction_id": "CAPTURE-2"}
                ]
            },
        }
    )

    assert refunded.action == GatewayWebhookAction.PAYMENT_REVERSED
    assert refunded.external_ref == "CAPTURE-1"
    assert disputed.action == GatewayWebhookAction.PAYMENT_DISPUTED
    assert disputed.external_ref == "CAPTURE-2"
