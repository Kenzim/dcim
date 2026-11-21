"""Refunds of payments (docs/billing-phase1-design.md 7.4).

Interfaces only; P1-R fills in the bodies. A refund is never marked succeeded
before the gateway confirms it.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.refund import Refund, RefundDestination, RefundReason


class RefundError(ValueError):
    pass


class RefundService:
    @staticmethod
    def request(
        db: Session,
        *,
        payment_id: int,
        amount_cents: int,
        destination: RefundDestination,
        reason: RefundReason,
        actor_user_id: Optional[int],
        idempotency_key: str,
        reason_text: Optional[str] = None,
        issue_credit_note: Optional[bool] = None,
        credit_note_lines: Optional[Sequence[Any]] = None,
    ) -> Refund:
        raise NotImplementedError("RefundService.request is implemented by P1-R")

    @staticmethod
    def complete(db: Session, refund_id: int, *, gateway_refund_ref: Optional[str] = None) -> Refund:
        """Mark a refund succeeded once the gateway has confirmed it."""
        raise NotImplementedError("RefundService.complete is implemented by P1-R")
