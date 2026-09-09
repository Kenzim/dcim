"""Payment gateway request/response logging with secret redaction."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.commerce_gateway_log import GatewayLogDirection, PaymentGatewayLog

_REDACT_KEY = re.compile(
    r"(authorization|password|secret|token|pan|card|client_secret|cvv)",
    re.IGNORECASE,
)


class GatewayLogService:
    @staticmethod
    def redact(obj: Any) -> Any:
        if isinstance(obj, dict):
            redacted: dict[str, Any] = {}
            for key, value in obj.items():
                if _REDACT_KEY.search(str(key)):
                    redacted[key] = "[REDACTED]"
                else:
                    redacted[key] = GatewayLogService.redact(value)
            return redacted
        if isinstance(obj, list):
            return [GatewayLogService.redact(item) for item in obj]
        return obj

    @staticmethod
    def _summarize(payload: Any, *, max_len: int = 8000) -> Optional[str]:
        if payload is None:
            return None
        try:
            if isinstance(payload, str):
                text = payload
            else:
                text = json.dumps(GatewayLogService.redact(payload), default=str)
        except (TypeError, ValueError):
            text = repr(payload)
        if len(text) > max_len:
            return text[: max_len - 3] + "..."
        return text

    @staticmethod
    def write(
        db: Session,
        *,
        gateway: str,
        direction: GatewayLogDirection,
        operation: str,
        payment_id: Optional[int] = None,
        invoice_id: Optional[int] = None,
        billing_account_id: Optional[int] = None,
        http_status: Optional[int] = None,
        request: Any = None,
        response: Any = None,
        correlation_id: Optional[str] = None,
    ) -> PaymentGatewayLog:
        row = PaymentGatewayLog(
            gateway=gateway[:64],
            direction=direction,
            operation=operation[:128],
            payment_id=payment_id,
            invoice_id=invoice_id,
            billing_account_id=billing_account_id,
            http_status=http_status,
            request_summary=GatewayLogService._summarize(request),
            response_summary=GatewayLogService._summarize(response),
            correlation_id=correlation_id[:128] if correlation_id else None,
        )
        db.add(row)
        db.flush()
        return row
