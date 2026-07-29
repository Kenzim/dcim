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
class GatewayRefundResult:
    status: GatewayResultStatus
    external_ref: Optional[str] = None
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


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

    def refund_payment(
        self,
        *,
        external_ref: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        reason: Optional[str] = None,
    ) -> GatewayRefundResult: ...

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
