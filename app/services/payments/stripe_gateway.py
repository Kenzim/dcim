"""Stripe implementation of the payment gateway contract."""

from __future__ import annotations

import importlib
from typing import Any, Mapping, Optional

from app.core.config import settings
from app.services.payments.base import (
    GatewayChargeResult,
    GatewayCustomer,
    GatewayNotConfiguredError,
    GatewayPaymentMethod,
    GatewayRefundResult,
    GatewayResultStatus,
    GatewaySetupIntent,
    GatewayWebhookAction,
    NormalizedGatewayWebhookEvent,
    PaymentGatewayError,
)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


class StripeGateway:
    name = "stripe"

    def __init__(
        self,
        *,
        secret_key: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        stripe_module: Any = None,
    ) -> None:
        self._secret_key = secret_key or settings.stripe_secret_key
        self._webhook_secret = webhook_secret or settings.stripe_webhook_secret
        if not self._secret_key:
            raise GatewayNotConfiguredError("Stripe secret key is not configured")
        self._stripe = stripe_module or importlib.import_module("stripe")

    def create_customer(
        self, *, reseller_id: int, email: Optional[str]
    ) -> GatewayCustomer:
        params: dict[str, Any] = {
            "metadata": {"rackflow_reseller_id": str(reseller_id)},
            "api_key": self._secret_key,
            "idempotency_key": f"rackflow-reseller-customer-{reseller_id}",
        }
        if email:
            params["email"] = email
        customer = self._stripe.Customer.create(**params)
        return GatewayCustomer(external_ref=str(_value(customer, "id")))

    def create_setup_intent(self, *, customer_ref: str) -> GatewaySetupIntent:
        intent = self._stripe.SetupIntent.create(
            customer=customer_ref,
            payment_method_types=["card"],
            usage="off_session",
            api_key=self._secret_key,
        )
        return GatewaySetupIntent(
            external_ref=str(_value(intent, "id")),
            client_secret=str(_value(intent, "client_secret")),
        )

    def get_payment_method(
        self, *, method_ref: str
    ) -> GatewayPaymentMethod:
        method = self._stripe.PaymentMethod.retrieve(
            method_ref, api_key=self._secret_key
        )
        return self._payment_method_result(method)

    def list_payment_methods(
        self, *, customer_ref: str
    ) -> list[GatewayPaymentMethod]:
        methods = self._stripe.PaymentMethod.list(
            customer=customer_ref,
            type="card",
            api_key=self._secret_key,
        )
        return [
            self._payment_method_result(method)
            for method in (_value(methods, "data", []) or [])
        ]

    @staticmethod
    def _payment_method_result(method: Any) -> GatewayPaymentMethod:
        customer = _value(method, "customer")
        if customer is not None and not isinstance(customer, str):
            customer = _value(customer, "id")
        card = _value(method, "card", {}) or {}
        brand = _value(card, "brand")
        last4 = _value(card, "last4")
        label = (
            f"{str(brand).title()} •••• {last4}"
            if brand and last4
            else str(_value(method, "type", "payment method")).title()
        )
        return GatewayPaymentMethod(
            external_ref=str(_value(method, "id")),
            customer_ref=str(customer or ""),
            method_type=str(_value(method, "type", "unknown")),
            label=label,
            brand=str(brand) if brand else None,
            last4=str(last4) if last4 else None,
        )

    def detach_payment_method(self, *, method_ref: str) -> None:
        self._stripe.PaymentMethod.detach(
            method_ref, api_key=self._secret_key
        )

    def charge_off_session(
        self,
        *,
        customer_ref: str,
        method_ref: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        metadata: Mapping[str, str],
    ) -> GatewayChargeResult:
        if type(amount_cents) is not int or amount_cents <= 0:
            raise PaymentGatewayError("Stripe amount must be positive integer cents")
        if currency.upper() != "USD":
            raise PaymentGatewayError("Stripe reseller payments support USD only")
        try:
            intent = self._stripe.PaymentIntent.create(
                amount=amount_cents,
                currency="usd",
                customer=customer_ref,
                payment_method=method_ref,
                off_session=True,
                confirm=True,
                metadata=dict(metadata),
                api_key=self._secret_key,
                idempotency_key=idempotency_key,
            )
            return self._intent_result(intent)
        except self._stripe.error.CardError as exc:
            error = getattr(exc, "error", None)
            intent = getattr(error, "payment_intent", None)
            if intent is not None:
                result = self._intent_result(intent)
                if result.status == GatewayResultStatus.REQUIRES_ACTION:
                    return result
            return GatewayChargeResult(
                status=GatewayResultStatus.DECLINED,
                external_ref=(
                    str(_value(intent, "id")) if intent is not None else None
                ),
                failure_code=str(getattr(exc, "code", None) or "card_declined"),
                failure_message="The payment method was declined",
            )
        except (
            self._stripe.error.APIConnectionError,
            self._stripe.error.APIError,
            self._stripe.error.RateLimitError,
        ) as exc:
            return GatewayChargeResult(
                status=GatewayResultStatus.TRANSIENT_ERROR,
                failure_code=type(exc).__name__,
                failure_message="The payment gateway is temporarily unavailable",
            )
        except self._stripe.error.StripeError as exc:
            return GatewayChargeResult(
                status=GatewayResultStatus.DECLINED,
                failure_code=str(getattr(exc, "code", None) or "stripe_error"),
                failure_message="Stripe rejected the payment request",
            )

    @staticmethod
    def _intent_result(intent: Any) -> GatewayChargeResult:
        status = str(_value(intent, "status", ""))
        external_ref = str(_value(intent, "id"))
        if status == "succeeded":
            return GatewayChargeResult(
                status=GatewayResultStatus.SUCCEEDED,
                external_ref=external_ref,
            )
        if status in {"requires_action", "requires_source_action"}:
            return GatewayChargeResult(
                status=GatewayResultStatus.REQUIRES_ACTION,
                external_ref=external_ref,
                client_secret=str(_value(intent, "client_secret")),
            )
        last_error = _value(intent, "last_payment_error", {}) or {}
        if status in {"processing", "requires_confirmation"}:
            mapped = GatewayResultStatus.TRANSIENT_ERROR
        else:
            mapped = GatewayResultStatus.DECLINED
        return GatewayChargeResult(
            status=mapped,
            external_ref=external_ref,
            failure_code=_value(last_error, "code") or status or "payment_failed",
            failure_message="The payment could not be completed",
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
        if not external_ref or not external_ref.strip():
            raise PaymentGatewayError("Stripe payment reference is required")
        if type(amount_cents) is not int or amount_cents <= 0:
            raise PaymentGatewayError("Stripe amount must be positive integer cents")
        if currency.upper() != "USD":
            raise PaymentGatewayError("Stripe reseller payments support USD only")
        params: dict[str, Any] = {
            "payment_intent": external_ref.strip(),
            "amount": amount_cents,
            "api_key": self._secret_key,
            "idempotency_key": idempotency_key,
        }
        if reason:
            params["reason"] = "requested_by_customer"
            params["metadata"] = {"rackflow_reason": reason[:500]}
        try:
            refund = self._stripe.Refund.create(**params)
        except (
            self._stripe.error.APIConnectionError,
            self._stripe.error.APIError,
            self._stripe.error.RateLimitError,
        ) as exc:
            return GatewayRefundResult(
                status=GatewayResultStatus.TRANSIENT_ERROR,
                failure_code=type(exc).__name__,
                failure_message="The payment gateway is temporarily unavailable",
            )
        except self._stripe.error.InvalidRequestError as exc:
            return GatewayRefundResult(
                status=GatewayResultStatus.DECLINED,
                failure_code=str(getattr(exc, "code", None) or "invalid_request"),
                failure_message="Stripe rejected the refund request",
            )
        except self._stripe.error.StripeError as exc:
            return GatewayRefundResult(
                status=GatewayResultStatus.DECLINED,
                failure_code=str(getattr(exc, "code", None) or "stripe_error"),
                failure_message="Stripe rejected the refund request",
            )
        status = str(_value(refund, "status", "")).lower()
        refund_id = str(_value(refund, "id") or "") or None
        if status in {"succeeded", "pending"}:
            return GatewayRefundResult(
                status=GatewayResultStatus.SUCCEEDED,
                external_ref=refund_id,
                metadata={"stripe_refund_status": status},
            )
        return GatewayRefundResult(
            status=GatewayResultStatus.DECLINED,
            external_ref=refund_id,
            failure_code=status or "refund_failed",
            failure_message="Stripe could not complete the refund",
        )

    def construct_webhook_event(
        self,
        *,
        payload: bytes,
        signature: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]:
        if not self._webhook_secret:
            raise GatewayNotConfiguredError(
                "Stripe webhook secret is not configured"
            )
        if not signature and headers:
            signature = headers.get("stripe-signature") or headers.get(
                "Stripe-Signature"
            )
        if not signature:
            raise PaymentGatewayError("Stripe webhook signature is missing")
        return self._stripe.Webhook.construct_event(
            payload, signature, self._webhook_secret
        )

    def normalize_webhook_event(
        self, event: Mapping[str, Any]
    ) -> NormalizedGatewayWebhookEvent:
        event_id = str(_value(event, "id", ""))
        event_type = str(_value(event, "type", ""))
        data = _value(event, "data", {}) or {}
        obj = _value(data, "object", {}) or {}
        action = GatewayWebhookAction.IGNORED
        external_ref: Optional[str] = None
        amount_cents: Optional[int] = None
        currency: Optional[str] = None
        failure_code: Optional[str] = None

        if event_type == "payment_intent.succeeded":
            action = GatewayWebhookAction.PAYMENT_COMPLETED
            external_ref = str(_value(obj, "id", ""))
            amount = _value(obj, "amount_received", _value(obj, "amount"))
            amount_cents = int(amount) if amount is not None else None
            currency = str(_value(obj, "currency", "")).upper() or None
        elif event_type == "payment_intent.payment_failed":
            action = GatewayWebhookAction.PAYMENT_FAILED
            external_ref = str(_value(obj, "id", ""))
            last_error = _value(obj, "last_payment_error", {}) or {}
            failure_code = str(
                _value(last_error, "code", "payment_failed")
            )
        elif event_type == "charge.refunded" or (
            event_type == "refund.updated"
            and str(_value(obj, "status", "")) == "succeeded"
        ):
            action = GatewayWebhookAction.PAYMENT_REVERSED
            external_ref = self._intent_ref(obj)
        elif event_type == "charge.dispute.created":
            action = GatewayWebhookAction.PAYMENT_DISPUTED
            external_ref = self._intent_ref(obj)

        return NormalizedGatewayWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            action=action,
            external_ref=external_ref,
            amount_cents=amount_cents,
            currency=currency,
            failure_code=failure_code,
        )

    @staticmethod
    def _intent_ref(obj: Any) -> str:
        payment_intent = _value(obj, "payment_intent")
        if payment_intent is not None:
            if not isinstance(payment_intent, str):
                payment_intent = _value(payment_intent, "id")
            return str(payment_intent or "")
        return str(_value(obj, "id", ""))
