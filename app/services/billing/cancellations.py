"""Service cancellation requests (docs/billing-phase2-design.md 10.6)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.billing_jobs import CancellationRequest


class CancellationService:
    @staticmethod
    def request(
        db: Session,
        *,
        service_id: int,
        type: str,
        reason: Optional[str],
        actor_user_id: Optional[int],
        source: str,
        idempotency_key: str,
        invoice_action: str = "cancel_unpaid",
    ) -> CancellationRequest:
        raise NotImplementedError("CancellationService.request is built by the B5, B7, B8 worker")

    @staticmethod
    def withdraw(db: Session, request_id: int, *, actor_user_id: Optional[int]) -> CancellationRequest:
        raise NotImplementedError("CancellationService.withdraw is built by the B5, B7, B8 worker")


def cancel_unpaid_invoices(
    db: Session,
    service_id: int,
    *,
    from_period_start: Optional[datetime],
    cause: str,
) -> list[int]:
    raise NotImplementedError("cancel_unpaid_invoices is built by the B5, B7, B8 worker")
