"""Signed outbound webhook enqueueing for commerce events."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.commerce_webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEndpoint,
)

logger = logging.getLogger(__name__)

COMMERCE_WEBHOOK_EVENTS = frozenset(
    {"order.paid", "invoice.paid", "ticket.created"}
)


class CommerceWebhookService:
    @staticmethod
    def _endpoint_wants(endpoint: WebhookEndpoint, event: str) -> bool:
        subscribed = endpoint.events or []
        if not subscribed:
            return True
        return event in subscribed

    @staticmethod
    def sign_payload(secret_hash: str, body: bytes) -> str:
        return hmac.new(
            secret_hash.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def enqueue(
        db: Session,
        event: str,
        payload: dict[str, Any],
    ) -> list[WebhookDelivery]:
        if event not in COMMERCE_WEBHOOK_EVENTS:
            return []
        endpoints = list(
            db.execute(
                select(WebhookEndpoint).where(WebhookEndpoint.enabled.is_(True))
            ).scalars()
        )
        deliveries: list[WebhookDelivery] = []
        for endpoint in endpoints:
            if not CommerceWebhookService._endpoint_wants(endpoint, event):
                continue
            delivery = WebhookDelivery(
                endpoint_id=endpoint.id,
                event=event,
                payload=payload,
                status=WebhookDeliveryStatus.PENDING,
            )
            db.add(delivery)
            deliveries.append(delivery)
        if deliveries:
            db.flush()
        return deliveries

    @staticmethod
    def deliver_pending(db: Session, *, limit: int = 50) -> int:
        """Best-effort synchronous delivery for pending rows (worker stub)."""
        rows = list(
            db.execute(
                select(WebhookDelivery)
                .where(WebhookDelivery.status == WebhookDeliveryStatus.PENDING)
                .order_by(WebhookDelivery.id)
                .limit(limit)
            ).scalars()
        )
        sent = 0
        for delivery in rows:
            endpoint = delivery.endpoint
            if endpoint is None or not endpoint.enabled:
                delivery.status = WebhookDeliveryStatus.FAILED
                delivery.last_error = "Endpoint disabled or missing"
                continue
            body = json.dumps(
                {"event": delivery.event, "payload": delivery.payload},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            signature = CommerceWebhookService.sign_payload(
                endpoint.secret_hash, body
            )
            delivery.attempts += 1
            try:
                response = httpx.post(
                    endpoint.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Rackflow-Signature": signature,
                        "X-Rackflow-Event": delivery.event,
                    },
                    timeout=10.0,
                )
                if 200 <= response.status_code < 300:
                    delivery.status = WebhookDeliveryStatus.DELIVERED
                    delivery.last_error = None
                    sent += 1
                else:
                    delivery.status = WebhookDeliveryStatus.FAILED
                    delivery.last_error = (
                        f"HTTP {response.status_code}: {response.text[:500]}"
                    )
            except Exception as exc:
                delivery.status = WebhookDeliveryStatus.FAILED
                delivery.last_error = str(exc)[:500]
                logger.warning(
                    "Webhook delivery %s failed: %s", delivery.id, exc, exc_info=True
                )
        if rows:
            db.flush()
        return sent

    @staticmethod
    def create_endpoint(
        db: Session,
        *,
        url: str,
        secret: str,
        events: Optional[list[str]] = None,
        enabled: bool = True,
    ) -> WebhookEndpoint:
        secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        row = WebhookEndpoint(
            url=url.strip(),
            secret_hash=secret_hash,
            events=events or [],
            enabled=enabled,
        )
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def serialize_endpoint(row: WebhookEndpoint) -> dict:
        return {
            "id": row.id,
            "url": row.url,
            "events": row.events or [],
            "enabled": row.enabled,
            "created_at": row.created_at,
        }
