"""Payment gateway registry used by APIs and billing orchestration."""

from __future__ import annotations

from typing import Optional

from app.core.config import settings
from app.services.payments.base import (
    GatewayNotConfiguredError,
    PaymentGateway,
)


class PaymentGatewayRegistry:
    def __init__(self) -> None:
        self._gateways: dict[str, PaymentGateway] = {}

    def register(self, gateway: PaymentGateway) -> None:
        self._gateways[gateway.name.lower()] = gateway

    def unregister(self, name: str) -> None:
        self._gateways.pop(name.lower(), None)

    def get(self, name: str) -> PaymentGateway:
        gateway = self._gateways.get(name.lower())
        if gateway is None:
            raise GatewayNotConfiguredError(
                f"Payment gateway '{name}' is not configured"
            )
        return gateway

    def maybe_get(self, name: str) -> Optional[PaymentGateway]:
        return self._gateways.get(name.lower())


payment_gateway_registry = PaymentGatewayRegistry()

if settings.stripe_enabled:
    from app.services.payments.stripe_gateway import StripeGateway

    payment_gateway_registry.register(StripeGateway())

if settings.paypal_enabled:
    from app.services.payments.paypal_gateway import PayPalGateway

    payment_gateway_registry.register(PayPalGateway())
