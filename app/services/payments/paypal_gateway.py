"""PayPal vault, off-session order, and webhook gateway implementation."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

import httpx

from app.core.config import settings
from app.services.payments.base import (
    GatewayChargeResult,
    GatewayCustomer,
    GatewayNotConfiguredError,
    GatewayPaymentMethod,
    GatewayRefundResult,
    GatewayResultStatus,
    GatewaySetupIntent,
    GatewayVaultSetup,
    GatewayWebhookAction,
    NormalizedGatewayWebhookEvent,
    PaymentGatewayError,
)

_SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
_LIVE_BASE_URL = "https://api-m.paypal.com"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=10.0, write=10.0, pool=5.0)
_JSON_CONTENT_TYPE = "application/json"
_GATEWAY_UNAVAILABLE = "The payment gateway is temporarily unavailable"
_PAYPAL_DECLINED = "PayPal declined the payment"
_TRANSIENT_STATUS_CODES = {408, 409, 425, 429}
_DECLINE_ISSUES = {
    "INSTRUMENT_DECLINED",
    "PAYER_CANNOT_PAY",
    "PAYMENT_DENIED",
    "TRANSACTION_REFUSED",
}
_PAYER_ACTION_ISSUES = {"PAYER_ACTION_REQUIRED", "CONTINGENCY"}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _links(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in (value or []) if isinstance(item, Mapping)]


def _link(data: Mapping[str, Any], *relations: str) -> Optional[str]:
    wanted = set(relations)
    for item in _links(data.get("links")):
        if str(item.get("rel", "")).lower() in wanted and item.get("href"):
            return str(item["href"])
    return None


def _request_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:48]
    return f"rackflow-{prefix}-{digest}"


def _money_to_cents(value: Any) -> int:
    try:
        cents = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError) as exc:
        raise PaymentGatewayError("PayPal returned an invalid amount") from exc
    if cents != cents.to_integral_value():
        raise PaymentGatewayError("PayPal returned an invalid amount precision")
    return int(cents)


class PayPalGateway:
    name = "paypal"

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        webhook_id: Optional[str] = None,
        environment: Optional[str] = None,
        return_base_url: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._client_id = client_id or settings.paypal_client_id
        self._client_secret = client_secret or settings.paypal_client_secret
        self._webhook_id = webhook_id or settings.paypal_webhook_id
        self._environment = environment or settings.paypal_environment
        self._return_base_url = (
            return_base_url
            or settings.paypal_return_base_url
            or settings.public_app_url
            or settings.public_base_url
        )
        if not self._client_id or not self._client_secret:
            raise GatewayNotConfiguredError(
                "PayPal client credentials are not configured"
            )
        if self._environment not in {"sandbox", "live"}:
            raise GatewayNotConfiguredError(
                "PayPal environment must be sandbox or live"
            )
        self._base_url = (
            _LIVE_BASE_URL
            if self._environment == "live"
            else _SANDBOX_BASE_URL
        )
        self._client = http_client or httpx.Client(timeout=_TIMEOUT)
        self._token_lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._access_token_expires_at = 0.0

    def _get_access_token(self, *, force_refresh: bool = False) -> str:
        with self._token_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._access_token
                and now < self._access_token_expires_at
            ):
                return self._access_token
            try:
                response = self._client.post(
                    f"{self._base_url}/v1/oauth2/token",
                    auth=(self._client_id, self._client_secret),
                    data={"grant_type": "client_credentials"},
                    headers={
                        "Accept": _JSON_CONTENT_TYPE,
                        "Accept-Language": "en_US",
                    },
                    timeout=_TIMEOUT,
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                raise PaymentGatewayError(
                    "PayPal authentication is temporarily unavailable"
                ) from exc
            data = self._response_json(response)
            token = data.get("access_token")
            if response.status_code != 200 or not token:
                raise PaymentGatewayError("PayPal authentication failed")
            try:
                expires_in = max(1, int(data.get("expires_in", 300)))
            except (TypeError, ValueError):
                expires_in = 300
            self._access_token = str(token)
            self._access_token_expires_at = now + max(1, expires_in - 60)
            return self._access_token

    @staticmethod
    def _response_json(response: httpx.Response) -> Mapping[str, Any]:
        try:
            return _mapping(response.json())
        except ValueError:
            return {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, str]] = None,
        request_id: Optional[str] = None,
    ) -> tuple[httpx.Response, Mapping[str, Any]]:
        for attempt in range(2):
            token = self._get_access_token(force_refresh=attempt == 1)
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": _JSON_CONTENT_TYPE,
                "Content-Type": _JSON_CONTENT_TYPE,
            }
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            if path.startswith("/v2/checkout/orders"):
                headers["Prefer"] = "return=representation"
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=_TIMEOUT,
            )
            if response.status_code != 401 or attempt == 1:
                return response, self._response_json(response)
            with self._token_lock:
                self._access_token = None
                self._access_token_expires_at = 0.0
        raise PaymentGatewayError("PayPal request failed")

    @staticmethod
    def _issue(data: Mapping[str, Any], default: str) -> str:
        details = data.get("details")
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, Mapping) and detail.get("issue"):
                    return str(detail["issue"])
        return str(data.get("name") or default)

    def create_customer(
        self, *, reseller_id: int, email: Optional[str]
    ) -> GatewayCustomer:
        raise PaymentGatewayError(
            "PayPal customers are created by the approved vault setup flow"
        )

    def create_setup_intent(self, *, customer_ref: str) -> GatewaySetupIntent:
        raise PaymentGatewayError("PayPal uses vault setup tokens")

    def create_vault_setup(
        self,
        *,
        reseller_id: int,
        customer_ref: Optional[str] = None,
    ) -> GatewayVaultSetup:
        if not self._return_base_url:
            raise GatewayNotConfiguredError(
                "PayPal public return base URL is not configured"
            )
        base = self._return_base_url.rstrip("/")
        customer: dict[str, str] = {
            "merchant_customer_id": f"rackflow-reseller-{reseller_id}"
        }
        if customer_ref:
            customer["id"] = customer_ref
        body = {
            "customer": customer,
            "payment_source": {
                "paypal": {
                    "description": "Rackflow saved payment method",
                    "permit_multiple_payment_tokens": True,
                    "usage_pattern": "IMMEDIATE",
                    "usage_type": "MERCHANT",
                    "customer_type": "CONSUMER",
                    "experience_context": {
                        "return_url": (
                            f"{base}/reseller/payments/paypal/return"
                        ),
                        "cancel_url": (
                            f"{base}/reseller/payments/paypal/cancel"
                        ),
                    },
                }
            },
        }
        try:
            response, data = self._request(
                "POST",
                "/v3/vault/setup-tokens",
                json_body=body,
                request_id=_request_id("setup", str(uuid.uuid4())),
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise PaymentGatewayError(
                "PayPal vault setup is temporarily unavailable"
            ) from exc
        if response.status_code not in {200, 201}:
            raise PaymentGatewayError("PayPal rejected the vault setup request")
        setup_token_id = str(data.get("id") or "")
        approval_url = _link(data, "approve", "payer-action")
        if not setup_token_id or not approval_url:
            raise PaymentGatewayError(
                "PayPal did not return a usable vault approval link"
            )
        paypal_customer = _mapping(data.get("customer"))
        return GatewayVaultSetup(
            external_ref=setup_token_id,
            approval_url=approval_url,
            customer_ref=(
                str(paypal_customer["id"])
                if paypal_customer.get("id")
                else None
            ),
        )

    def complete_vault_setup(
        self, *, setup_token_ref: str
    ) -> GatewayPaymentMethod:
        body = {
            "payment_source": {
                "token": {"id": setup_token_ref, "type": "SETUP_TOKEN"}
            }
        }
        try:
            response, data = self._request(
                "POST",
                "/v3/vault/payment-tokens",
                json_body=body,
                request_id=_request_id("complete", setup_token_ref),
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise PaymentGatewayError(
                "PayPal vault completion is temporarily unavailable"
            ) from exc
        if response.status_code not in {200, 201}:
            raise PaymentGatewayError(
                "PayPal setup token is not approved or is no longer valid"
            )
        method = self._payment_method_result(data)
        if not method.external_ref:
            raise PaymentGatewayError("PayPal did not return a payment token")
        return method

    @staticmethod
    def _payment_method_result(
        data: Mapping[str, Any],
    ) -> GatewayPaymentMethod:
        customer = _mapping(data.get("customer"))
        payment_source = _mapping(data.get("payment_source"))
        if "paypal" not in payment_source:
            raise PaymentGatewayError(
                "PayPal payment token is not a PayPal wallet"
            )
        source = _mapping(payment_source.get("paypal"))
        email = str(source.get("email_address") or "").strip()
        name = _mapping(source.get("name"))
        full_name = str(name.get("full_name") or "").strip()
        label_detail = email or full_name
        label = f"PayPal ({label_detail})" if label_detail else "PayPal"
        metadata: dict[str, Any] = {}
        if customer.get("merchant_customer_id"):
            metadata["merchant_customer_id"] = str(
                customer["merchant_customer_id"]
            )
        if email:
            metadata["payer_email"] = email
        return GatewayPaymentMethod(
            external_ref=str(data.get("id") or ""),
            customer_ref=(
                str(customer["id"]) if customer.get("id") else None
            ),
            method_type="paypal",
            label=label,
            brand="paypal",
            metadata=metadata,
        )

    def get_payment_method(
        self, *, method_ref: str
    ) -> GatewayPaymentMethod:
        try:
            response, data = self._request(
                "GET", f"/v3/vault/payment-tokens/{method_ref}"
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise PaymentGatewayError(
                "PayPal payment token lookup is temporarily unavailable"
            ) from exc
        if response.status_code != 200:
            raise PaymentGatewayError("PayPal payment token was not found")
        method = self._payment_method_result(data)
        if method.external_ref != method_ref:
            raise PaymentGatewayError("PayPal returned an unexpected payment token")
        return method

    def list_payment_methods(
        self, *, customer_ref: str
    ) -> list[GatewayPaymentMethod]:
        try:
            response, data = self._request(
                "GET",
                "/v3/vault/payment-tokens",
                params={"customer_id": customer_ref},
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise PaymentGatewayError(
                "PayPal payment token lookup is temporarily unavailable"
            ) from exc
        if response.status_code != 200:
            raise PaymentGatewayError("PayPal payment tokens could not be listed")
        return [
            self._payment_method_result(item)
            for item in data.get("payment_tokens", [])
            if isinstance(item, Mapping)
        ]

    def detach_payment_method(self, *, method_ref: str) -> None:
        try:
            response, _data = self._request(
                "DELETE",
                f"/v3/vault/payment-tokens/{method_ref}",
                request_id=_request_id("delete", method_ref),
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise PaymentGatewayError(
                "PayPal payment token deletion is temporarily unavailable"
            ) from exc
        if response.status_code not in {200, 204, 404}:
            raise PaymentGatewayError(
                "PayPal rejected the payment token deletion"
            )

    def _refund_result_from_response(self, response, data: dict[str, Any]) -> GatewayRefundResult:
        if response.status_code in {200, 201}:
            status = str(data.get("status") or "").upper()
            refund_id = str(data.get("id") or "") or None
            if status in {"COMPLETED", "PENDING"}:
                return GatewayRefundResult(
                    status=GatewayResultStatus.SUCCEEDED,
                    external_ref=refund_id,
                    metadata={"paypal_refund_status": status.lower()},
                )
            return GatewayRefundResult(
                status=GatewayResultStatus.DECLINED,
                external_ref=refund_id,
                failure_code=status.lower() or "refund_failed",
                failure_message=_PAYPAL_DECLINED,
            )
        issue = self._issue(data, "refund_failed")
        if response.status_code in _TRANSIENT_STATUS_CODES or response.status_code >= 500:
            return GatewayRefundResult(
                status=GatewayResultStatus.TRANSIENT_ERROR,
                failure_code=issue.lower(),
                failure_message=_GATEWAY_UNAVAILABLE,
            )
        return GatewayRefundResult(
            status=GatewayResultStatus.DECLINED,
            failure_code=issue.lower(),
            failure_message=_PAYPAL_DECLINED,
        )

    def refund_payment(
        self,
        *,
        external_ref: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        reason: Optional[str] = None,
    ) -> GatewayRefundResult:
        capture_id = (external_ref or "").strip()
        if not capture_id:
            raise PaymentGatewayError("PayPal capture reference is required")
        if type(amount_cents) is not int or amount_cents <= 0:
            raise PaymentGatewayError(
                "PayPal amount must be positive integer cents"
            )
        if currency.upper() != "USD":
            raise PaymentGatewayError("PayPal reseller payments support USD only")
        body: dict[str, Any] = {
            "amount": {
                "currency_code": "USD",
                "value": f"{amount_cents // 100}.{amount_cents % 100:02d}",
            }
        }
        if reason:
            body["note_to_payer"] = reason[:255]
        try:
            response, data = self._request(
                "POST",
                f"/v2/payments/captures/{capture_id}/refund",
                json_body=body,
                request_id=_request_id("refund", idempotency_key),
            )
        except (httpx.TimeoutException, httpx.RequestError):
            return GatewayRefundResult(
                status=GatewayResultStatus.TRANSIENT_ERROR,
                failure_code="paypal_unavailable",
                failure_message=_GATEWAY_UNAVAILABLE,
            )
        return self._refund_result_from_response(response, data)

    def charge_off_session(
        self,
        *,
        customer_ref: Optional[str],
        method_ref: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        metadata: Mapping[str, str],
    ) -> GatewayChargeResult:
        if type(amount_cents) is not int or amount_cents <= 0:
            raise PaymentGatewayError(
                "PayPal amount must be positive integer cents"
            )
        if customer_ref is not None and not customer_ref.strip():
            raise PaymentGatewayError("PayPal customer reference is invalid")
        if currency.upper() != "USD":
            raise PaymentGatewayError("PayPal reseller payments support USD only")
        body = self._order_body(
            method_ref=method_ref,
            amount_cents=amount_cents,
            metadata=metadata,
        )
        try:
            response, data = self._request(
                "POST",
                "/v2/checkout/orders",
                json_body=body,
                request_id=_request_id("order", idempotency_key),
            )
        except (httpx.TimeoutException, httpx.RequestError):
            return GatewayChargeResult(
                status=GatewayResultStatus.TRANSIENT_ERROR,
                failure_code="paypal_unavailable",
                failure_message=_GATEWAY_UNAVAILABLE,
            )
        if response.status_code not in {200, 201}:
            return self._http_failure_result(response.status_code, data)
        return self._order_result(
            data,
            amount_cents=amount_cents,
            currency=currency,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _order_body(
        *,
        method_ref: str,
        amount_cents: int,
        metadata: Mapping[str, str],
    ) -> Mapping[str, Any]:
        purchase_unit: dict[str, Any] = {
            "amount": {
                "currency_code": "USD",
                "value": (
                    f"{amount_cents // 100}.{amount_cents % 100:02d}"
                ),
            }
        }
        invoice_id = metadata.get("rackflow_invoice_id")
        if invoice_id:
            purchase_unit["custom_id"] = invoice_id[:127]
        return {
            "intent": "CAPTURE",
            "purchase_units": [purchase_unit],
            "payment_source": {
                "paypal": {
                    "vault_id": method_ref,
                    "stored_credential": {
                        "payment_initiator": "MERCHANT",
                        "usage": "SUBSEQUENT",
                        "usage_pattern": "UNSCHEDULED_POSTPAID",
                    },
                }
            },
        }

    def _order_result(
        self,
        data: Mapping[str, Any],
        *,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
    ) -> GatewayChargeResult:
        order_id = str(data.get("id") or "")
        order_status = str(data.get("status") or "").upper()
        if order_status == "COMPLETED":
            return self._completed_capture_result(
                data, amount_cents=amount_cents, currency=currency
            )
        if order_status == "APPROVED":
            return self._capture_approved_order(
                order_id=order_id,
                amount_cents=amount_cents,
                currency=currency,
                idempotency_key=idempotency_key,
            )
        approval_url = _link(data, "payer-action", "approve")
        if order_status == "PAYER_ACTION_REQUIRED" or approval_url:
            return GatewayChargeResult(
                status=GatewayResultStatus.REQUIRES_ACTION,
                external_ref=order_id or None,
                client_secret=approval_url,
                failure_code="payer_action_required",
                failure_message="The payer must approve this PayPal payment",
            )
        if order_status in {"DECLINED", "DENIED", "FAILED", "VOIDED"}:
            return GatewayChargeResult(
                status=GatewayResultStatus.DECLINED,
                external_ref=order_id or None,
                failure_code=order_status.lower(),
                failure_message=_PAYPAL_DECLINED,
            )
        return GatewayChargeResult(
            status=GatewayResultStatus.TRANSIENT_ERROR,
            external_ref=order_id or None,
            failure_code=order_status.lower() or "order_not_completed",
            failure_message="The PayPal payment has not completed",
        )

    def _capture_approved_order(
        self,
        *,
        order_id: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
    ) -> GatewayChargeResult:
        try:
            response, data = self._request(
                "POST",
                f"/v2/checkout/orders/{order_id}/capture",
                json_body={},
                request_id=_request_id("capture", idempotency_key),
            )
        except (httpx.TimeoutException, httpx.RequestError):
            return GatewayChargeResult(
                status=GatewayResultStatus.TRANSIENT_ERROR,
                external_ref=order_id or None,
                failure_code="capture_unavailable",
                failure_message=_GATEWAY_UNAVAILABLE,
            )
        if response.status_code not in {200, 201}:
            return self._http_failure_result(response.status_code, data)
        return self._completed_capture_result(
            data, amount_cents=amount_cents, currency=currency
        )

    def _http_failure_result(
        self, status_code: int, data: Mapping[str, Any]
    ) -> GatewayChargeResult:
        issue = self._issue(data, "paypal_error")
        if issue in _PAYER_ACTION_ISSUES:
            return GatewayChargeResult(
                status=GatewayResultStatus.REQUIRES_ACTION,
                client_secret=_link(data, "payer-action", "approve"),
                failure_code=issue.lower(),
                failure_message="The payer must approve this PayPal payment",
            )
        if status_code in _TRANSIENT_STATUS_CODES or status_code >= 500:
            result_status = GatewayResultStatus.TRANSIENT_ERROR
            message = _GATEWAY_UNAVAILABLE
        else:
            result_status = GatewayResultStatus.DECLINED
            message = _PAYPAL_DECLINED
        if issue in _DECLINE_ISSUES:
            result_status = GatewayResultStatus.DECLINED
        return GatewayChargeResult(
            status=result_status,
            failure_code=issue.lower(),
            failure_message=message,
        )

    @staticmethod
    def _completed_capture_result(
        data: Mapping[str, Any], *, amount_cents: int, currency: str
    ) -> GatewayChargeResult:
        captures = PayPalGateway._capture_objects(data)
        completed = PayPalGateway._capture_with_status(
            captures, {"COMPLETED"}
        )
        if completed is None:
            return PayPalGateway._incomplete_capture_result(captures)
        amount = _mapping(completed.get("amount"))
        captured_cents = _money_to_cents(amount.get("value"))
        captured_currency = str(amount.get("currency_code") or "").upper()
        capture_id = str(completed.get("id") or "")
        if captured_cents != amount_cents or captured_currency != currency.upper():
            return GatewayChargeResult(
                status=GatewayResultStatus.TRANSIENT_ERROR,
                external_ref=capture_id or None,
                failure_code="capture_amount_mismatch",
                failure_message="PayPal returned an unexpected capture amount",
            )
        if not capture_id:
            return GatewayChargeResult(
                status=GatewayResultStatus.TRANSIENT_ERROR,
                failure_code="capture_id_missing",
                failure_message="PayPal did not return a capture identifier",
            )
        return GatewayChargeResult(
            status=GatewayResultStatus.SUCCEEDED,
            external_ref=capture_id,
        )

    @staticmethod
    def _capture_objects(
        data: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        captures: list[Mapping[str, Any]] = []
        for unit in data.get("purchase_units", []):
            if not isinstance(unit, Mapping):
                continue
            payments = _mapping(unit.get("payments"))
            captures.extend(
                item
                for item in payments.get("captures", [])
                if isinstance(item, Mapping)
            )
        return captures

    @staticmethod
    def _capture_with_status(
        captures: list[Mapping[str, Any]], statuses: set[str]
    ) -> Optional[Mapping[str, Any]]:
        for capture in captures:
            status = str(capture.get("status") or "").upper()
            if status in statuses:
                return capture
        return None

    @staticmethod
    def _incomplete_capture_result(
        captures: list[Mapping[str, Any]],
    ) -> GatewayChargeResult:
        declined = PayPalGateway._capture_with_status(
            captures, {"DECLINED", "DENIED", "FAILED"}
        )
        if declined is not None:
            return GatewayChargeResult(
                status=GatewayResultStatus.DECLINED,
                external_ref=str(declined.get("id") or "") or None,
                failure_code="capture_declined",
                failure_message=_PAYPAL_DECLINED,
            )
        capture_ref = None
        if captures:
            capture_ref = str(captures[0].get("id") or "") or None
        return GatewayChargeResult(
            status=GatewayResultStatus.TRANSIENT_ERROR,
            external_ref=capture_ref,
            failure_code="capture_not_completed",
            failure_message="The PayPal capture has not completed",
        )

    def construct_webhook_event(
        self,
        *,
        payload: bytes,
        signature: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]:
        if not self._webhook_id:
            raise GatewayNotConfiguredError(
                "PayPal webhook ID is not configured"
            )
        lowered = {key.lower(): value for key, value in (headers or {}).items()}
        required = {
            "auth_algo": "paypal-auth-algo",
            "cert_url": "paypal-cert-url",
            "transmission_id": "paypal-transmission-id",
            "transmission_sig": "paypal-transmission-sig",
            "transmission_time": "paypal-transmission-time",
        }
        missing = [
            header for header in required.values() if not lowered.get(header)
        ]
        if missing:
            raise PaymentGatewayError(
                "Required PayPal webhook transmission headers are missing"
            )
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaymentGatewayError("Invalid PayPal webhook payload") from exc
        if not isinstance(event, Mapping):
            raise PaymentGatewayError("Invalid PayPal webhook payload")
        verification_body = {
            field: lowered[header] for field, header in required.items()
        }
        verification_body["webhook_id"] = self._webhook_id
        verification_body["webhook_event"] = event
        try:
            response, data = self._request(
                "POST",
                "/v1/notifications/verify-webhook-signature",
                json_body=verification_body,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise PaymentGatewayError(
                "PayPal webhook verification is temporarily unavailable"
            ) from exc
        if (
            response.status_code != 200
            or str(data.get("verification_status") or "").upper() != "SUCCESS"
        ):
            raise PaymentGatewayError("Invalid PayPal webhook signature")
        return event

    def normalize_webhook_event(
        self, event: Mapping[str, Any]
    ) -> NormalizedGatewayWebhookEvent:
        event_id = str(event.get("id") or "")
        event_type = str(event.get("event_type") or "")
        resource = _mapping(event.get("resource"))
        action = GatewayWebhookAction.IGNORED
        external_ref: Optional[str] = None
        related_external_ref: Optional[str] = None
        amount_cents: Optional[int] = None
        currency: Optional[str] = None
        failure_code: Optional[str] = None

        if event_type == "PAYMENT.CAPTURE.COMPLETED":
            action = GatewayWebhookAction.PAYMENT_COMPLETED
            external_ref = str(resource.get("id") or "")
            related_external_ref = self._related_order_id(resource)
            amount = _mapping(resource.get("amount"))
            amount_cents = _money_to_cents(amount.get("value"))
            currency = str(amount.get("currency_code") or "").upper() or None
        elif event_type == "PAYMENT.CAPTURE.DENIED":
            action = GatewayWebhookAction.PAYMENT_FAILED
            external_ref = str(resource.get("id") or "")
            related_external_ref = self._related_order_id(resource)
            details = _mapping(resource.get("status_details"))
            failure_code = str(details.get("reason") or "capture_denied")
        elif event_type in {
            "PAYMENT.CAPTURE.REFUNDED",
            "PAYMENT.CAPTURE.REVERSED",
        }:
            action = GatewayWebhookAction.PAYMENT_REVERSED
            external_ref = self._related_capture_id(resource)
        elif event_type == "CUSTOMER.DISPUTE.CREATED":
            action = GatewayWebhookAction.PAYMENT_DISPUTED
            external_ref = self._disputed_capture_id(resource)

        return NormalizedGatewayWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            action=action,
            external_ref=external_ref,
            related_external_ref=related_external_ref,
            amount_cents=amount_cents,
            currency=currency,
            failure_code=failure_code,
        )

    @staticmethod
    def _related_order_id(resource: Mapping[str, Any]) -> Optional[str]:
        supplementary = _mapping(resource.get("supplementary_data"))
        related_ids = _mapping(supplementary.get("related_ids"))
        order_id = related_ids.get("order_id")
        if order_id:
            return str(order_id)
        for item in _links(resource.get("links")):
            if str(item.get("rel") or "").lower() == "up":
                href = str(item.get("href") or "").rstrip("/")
                if href:
                    return href.rsplit("/", 1)[-1]
        return None

    @staticmethod
    def _related_capture_id(resource: Mapping[str, Any]) -> str:
        supplementary = _mapping(resource.get("supplementary_data"))
        related_ids = _mapping(supplementary.get("related_ids"))
        capture_id = related_ids.get("capture_id")
        if capture_id:
            return str(capture_id)
        for item in _links(resource.get("links")):
            if str(item.get("rel") or "").lower() in {"up", "capture"}:
                href = str(item.get("href") or "").rstrip("/")
                if href:
                    return href.rsplit("/", 1)[-1]
        return str(resource.get("id") or "")

    @staticmethod
    def _disputed_capture_id(resource: Mapping[str, Any]) -> str:
        transactions = resource.get("disputed_transactions")
        if isinstance(transactions, list):
            for transaction in transactions:
                if isinstance(transaction, Mapping):
                    reference = transaction.get("seller_transaction_id")
                    if reference:
                        return str(reference)
        return PayPalGateway._related_capture_id(resource)
