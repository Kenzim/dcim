"""Typed contracts shared by payment gateways and the billing orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol


class GatewayResultStatus(str, Enum):
    SUCCEEDED = "succeeded"
    REQUIRES_ACTION = "requires_action"
    DECLINED = "declined"
    TRANSIENT_ERROR = "transient_error"


class GatewayWebhookAction(str, Enum):
    PAYMENT_COMPLETED = "payment_completed"
    PAYMENT_FAILED = "payment_failed"
    PAYMENT_REVERSED = "payment_reversed"
    PAYMENT_DISPUTED = "payment_disputed"
    IGNORED = "ignored"


class PaymentGatewayError(RuntimeError):
    pass


class GatewayNotConfiguredError(PaymentGatewayError):
    pass


class PaymentMethodOwnershipError(PaymentGatewayError):
    pass


@dataclass(frozen=True)
class GatewayCustomer:
    external_ref: str


@dataclass(frozen=True)
class GatewaySetupIntent:
    external_ref: str
    client_secret: str


@dataclass(frozen=True)
class GatewayVaultSetup:
    external_ref: str
    approval_url: str
    customer_ref: Optional[str] = None


@dataclass(frozen=True)
class GatewayPaymentMethod:
    external_ref: str
    customer_ref: Optional[str]
    method_type: str
    label: Optional[str] = None
    brand: Optional[str] = None
    last4: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayChargeResult:
    status: GatewayResultStatus
    external_ref: Optional[str] = None
    client_secret: Optional[str] = None
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayPaymentIntent:
    """An on-session payment the client confirms in the browser."""

    external_ref: str
    client_secret: Optional[str]
    status: str
    amount_cents: Optional[int] = None
    currency: Optional[str] = None


@dataclass(frozen=True)
class GatewayRefundResult:
    """Outcome of a refund request or lookup.

    ``status`` SUCCEEDED means the gateway accepted the refund; ``pending``
    is then True while the gateway has not yet confirmed that the money
    moved, and the refund must not be recorded as succeeded until it has.
    """

    status: GatewayResultStatus
    external_ref: Optional[str] = None
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    pending: bool = False
    amount_cents: Optional[int] = None


@dataclass(frozen=True)
class NormalizedGatewayWebhookEvent:
    event_id: str
    event_type: str
    action: GatewayWebhookAction
    external_ref: Optional[str] = None
    related_external_ref: Optional[str] = None
    amount_cents: Optional[int] = None
    currency: Optional[str] = None
    failure_code: Optional[str] = None
    # Gateway id of the refund or dispute the event is about, when it names one.
    refund_ref: Optional[str] = None
    # Gateway status of that refund (for example succeeded, pending, failed).
    refund_status: Optional[str] = None
    # Total refunded on the payment so far, when the gateway gives only that.
    refunded_total_cents: Optional[int] = None


class PaymentGateway(Protocol):
    name: str

    def create_customer(
        self, *, reseller_id: int, email: Optional[str]
    ) -> GatewayCustomer: ...

    def create_setup_intent(self, *, customer_ref: str) -> GatewaySetupIntent: ...

    def get_payment_method(
        self, *, method_ref: str
    ) -> GatewayPaymentMethod: ...

    def list_payment_methods(
        self, *, customer_ref: str
    ) -> list[GatewayPaymentMethod]: ...

    def detach_payment_method(self, *, method_ref: str) -> None: ...

    def charge_off_session(
        self,
        *,
        customer_ref: Optional[str],
        method_ref: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        metadata: Mapping[str, str],
    ) -> GatewayChargeResult: ...

    def create_payment_intent(
        self,
        *,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        metadata: Mapping[str, str],
    ) -> GatewayPaymentIntent: ...

    def retrieve_payment_intent(
        self, *, external_ref: str
    ) -> GatewayPaymentIntent: ...

    def cancel_payment_intent(
        self, *, external_ref: str
    ) -> GatewayPaymentIntent: ...

    def refund_payment(
        self,
        *,
        external_ref: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        reason: Optional[str] = None,
    ) -> GatewayRefundResult: ...

    def lookup_refund(
        self,
        *,
        external_ref: str,
        idempotency_key: str,
        refund_ref: Optional[str] = None,
    ) -> Optional[GatewayRefundResult]:
        """Report a refund made earlier with ``idempotency_key`` (or ``refund_ref``).

        Returns None when the gateway has no such refund, and a
        TRANSIENT_ERROR result when it could not be asked.
        """
        ...

    def construct_webhook_event(
        self,
        *,
        payload: bytes,
        signature: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]: ...

    def normalize_webhook_event(
        self, event: Mapping[str, Any]
    ) -> NormalizedGatewayWebhookEvent: ...
