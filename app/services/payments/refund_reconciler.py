"""Refund reconciler (docs/billing-phase1-design.md 7.4).

Settles gateway refunds left ``pending``: after a timeout or an unknown
answer, a refund the gateway still reports as pending, and refunds reserved
for later dispatch. Each refund is claimed with a conditional update, the
claim is committed, the gateway is asked with no database lock held, and the
answer is recorded in a transaction of its own. A refund with no definitive
answer 24 hours after it was requested, or after six attempts, moves to
``review`` for staff (``app/api/billing_refunds_admin.py``).

Run by the recurring billing worker as the periodic job
``billing_refund_reconciler`` (``app/services/billing/periodic.py``), which is
off by default and has a dry run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.refund import Refund, RefundDestination, RefundReason, RefundStatus
from app.models.reseller import Payment
from app.services.payments.base import GatewayNotConfiguredError, GatewayRefundResult
from app.services.refund_service import (
    GATEWAY_RETRY_WINDOW,
    MAX_ATTEMPTS,
    REVIEW_AFTER,
    RefundService,
    _registry,
    aware,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
# Delay before the next attempt, by the number of attempts made so far.
BACKOFF = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
)

# Refunds the gateway made itself: Rackflow never sends these, it only asks.
_LOOKUP_ONLY = (RefundReason.GATEWAY_INITIATED, RefundReason.DISPUTE)


def next_attempt_after(refund: Refund, now: datetime) -> datetime:
    attempts = int(refund.attempts or 0)
    if 1 <= attempts <= len(BACKOFF):
        return now + BACKOFF[attempts - 1]
    return max(now + BACKOFF[-1], aware(refund.requested_at) + REVIEW_AFTER)


class RefundReconciler:
    @staticmethod
    def due(db: Session, now: datetime, limit: int = BATCH_SIZE) -> list[Refund]:
        return list(
            db.execute(
                select(Refund)
                .where(
                    Refund.status == RefundStatus.PENDING,
                    Refund.destination == RefundDestination.GATEWAY,
                    Refund.next_attempt_at.isnot(None),
                    Refund.next_attempt_at <= now,
                    (Refund.claim_token.is_(None)) | (Refund.claim_expires_at < now),
                )
                .order_by(Refund.next_attempt_at, Refund.id)
                .limit(limit)
            ).scalars()
        )

    @staticmethod
    def run(
        db: Session, now: datetime, *, dry_run: bool = False, registry: Any = None
    ) -> dict:
        """One pass. Commits per refund unless ``dry_run``, which only reports."""
        counts = {"seen": 0, "succeeded": 0, "failed": 0, "pending": 0, "review": 0, "skipped": 0}
        actions: list[dict] = []
        for refund in RefundReconciler.due(db, now):
            counts["seen"] += 1
            if dry_run:
                action = RefundReconciler._planned_action(refund, now)
                actions.append({"refund_id": refund.id, "action": action})
                logger.info("Refund reconciler (dry run): refund %s would %s", refund.id, action)
                continue
            result = RefundReconciler._reconcile_one(db, refund.id, now, registry)
            counts[result] += 1
        return {**counts, **({"planned": actions} if dry_run else {})}

    @staticmethod
    def _expired(refund: Refund, now: datetime) -> bool:
        return (
            now - aware(refund.requested_at) >= REVIEW_AFTER
            or int(refund.attempts or 0) >= MAX_ATTEMPTS
        )

    @staticmethod
    def _may_resend(refund: Refund, now: datetime) -> bool:
        return (
            refund.reason not in _LOOKUP_ONLY
            and now - aware(refund.requested_at) < GATEWAY_RETRY_WINDOW
        )

    @staticmethod
    def _planned_action(refund: Refund, now: datetime) -> str:
        if RefundReconciler._may_resend(refund, now):
            return "look up or re-send"
        if RefundReconciler._expired(refund, now):
            return "look up, else move to review"
        return "look up"

    @staticmethod
    def _reconcile_one(db: Session, refund_id: int, now: datetime, registry: Any) -> str:
        refund = db.get(Refund, refund_id)
        try:
            adapter = _registry(registry).get(refund.gateway)
        except GatewayNotConfiguredError:
            adapter = None
        payment = db.get(Payment, refund.payment_id)
        external_ref = payment.external_ref or ""
        key = refund.idempotency_key
        ref = refund.gateway_refund_ref
        may_resend = RefundReconciler._may_resend(refund, now)
        token = RefundService.claim(db, refund_id, now)
        if token is None:
            db.rollback()
            return "skipped"
        db.commit()

        outcome: Optional[GatewayRefundResult] = None
        if adapter is not None:
            outcome = RefundReconciler._ask(adapter, external_ref, key, ref, refund, may_resend)

        refund = RefundService.apply_outcome(db, refund_id, outcome, claim_token=token, now=now)
        if refund.status == RefundStatus.PENDING:
            refund = db.get(Refund, refund_id)
            if RefundReconciler._expired(refund, now):
                RefundService.move_to_review(db, refund_id, failure_code=refund.failure_code or "no_answer")
            else:
                refund.next_attempt_at = next_attempt_after(refund, now)
        db.commit()
        refund = db.get(Refund, refund_id)
        return {
            RefundStatus.SUCCEEDED: "succeeded",
            RefundStatus.FAILED: "failed",
            RefundStatus.REVIEW: "review",
        }.get(refund.status, "pending")

    @staticmethod
    def _ask(
        adapter: Any,
        external_ref: str,
        key: str,
        ref: Optional[str],
        refund: Refund,
        may_resend: bool,
    ) -> Optional[GatewayRefundResult]:
        """Ask the gateway about a refund; no database lock is held here."""
        lookup = getattr(adapter, "lookup_refund", None)
        try:
            if lookup is not None:
                found = lookup(external_ref=external_ref, idempotency_key=key, refund_ref=ref)
                if found is not None or not may_resend:
                    return found
            if not may_resend:
                return None
            # Same idempotency key, inside the gateway's retention: returns the
            # original refund instead of refunding twice.
            return RefundService._send(
                adapter,
                refund.id,
                {
                    "external_ref": external_ref,
                    "amount_cents": int(refund.amount_cents),
                    "currency": refund.currency,
                    "idempotency_key": key,
                    "reason": refund.reason_text,
                },
            )
        except Exception:  # noqa: BLE001 - an unknown answer leaves the refund pending
            logger.warning("Refund reconciler: gateway lookup for refund %s raised", refund.id, exc_info=True)
            return None
