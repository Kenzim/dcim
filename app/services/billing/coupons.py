"""Coupon rules that depend on stored usage, and usage counting at payment.

A coupon discounts the first invoice of an order only, whatever its
``duration``: renewals are charged at the plan's recurring price. Recurring
discounts for ``repeating`` and ``forever`` coupons are deferred to D6; until
then checkout accepts them and the quote and the invoice's discount line say
that the discount applies to the first invoice only.

Limits are checked without a lock when quoting, and again under the coupon
row lock when the usage is counted at payment. A limit exceeded at payment
(two unpaid orders that both passed the quote) does not undo the settlement:
the client has paid the discounted price, so the use is counted, logged and
audited as ``billing.coupon_limit_exceeded`` for review.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.commerce_coupon_tax import Coupon, CouponDuration, CouponRedemption
from app.models.commerce_order import Order
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

FIRST_INVOICE_ONLY_NOTE = "first invoice only"


class CouponRuleError(ValueError):
    pass


def coupon_by_code(db: Session, code: Optional[str]) -> Optional[Coupon]:
    if not code or not code.strip():
        return None
    return db.execute(
        select(Coupon).where(func.lower(Coupon.code) == code.strip().lower())
    ).scalar_one_or_none()


def account_redemptions(db: Session, coupon_id: int, billing_account_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(CouponRedemption.id)).where(
                CouponRedemption.coupon_id == coupon_id,
                CouponRedemption.billing_account_id == billing_account_id,
            )
        )
        or 0
    )


def first_invoice_only(coupon: Coupon) -> bool:
    """True when the coupon's duration promises more than the first invoice it gets."""
    return coupon.duration in {CouponDuration.REPEATING, CouponDuration.FOREVER}


def check_stored_rules(
    db: Session, coupon: Coupon, *, billing_account_id: Optional[int]
) -> None:
    """Raise CouponRuleError when the per-account limit or duration settings forbid use."""
    if coupon.duration == CouponDuration.REPEATING and not (
        coupon.duration_months and int(coupon.duration_months) > 0
    ):
        raise CouponRuleError("Coupon is not configured correctly")
    if (
        billing_account_id is not None
        and coupon.max_uses_per_account is not None
        and account_redemptions(db, coupon.id, billing_account_id)
        >= int(coupon.max_uses_per_account)
    ):
        raise CouponRuleError("Coupon usage limit reached for this account")


def record_coupon_usage(
    db: Session, *, order: Order, invoice_id: Optional[int] = None
) -> Optional[CouponRedemption]:
    """Count one use of the order's coupon. Idempotent per (coupon, order)."""
    coupon = coupon_by_code(db, order.coupon_code)
    if coupon is None:
        if order.coupon_code:
            logger.warning(
                "Order %s used coupon %s, which no longer exists", order.id, order.coupon_code
            )
        return None
    existing = db.execute(
        select(CouponRedemption).where(
            CouponRedemption.coupon_id == coupon.id,
            CouponRedemption.order_id == order.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    from app.services.billing_locks import BillingLocks

    locked = BillingLocks.coupon(db, coupon.id)
    exceeded = _limits_exceeded(db, locked, order.billing_account_id)
    redemption = CouponRedemption(
        coupon_id=locked.id,
        billing_account_id=order.billing_account_id,
        order_id=order.id,
        invoice_id=invoice_id,
    )
    try:
        with db.begin_nested():
            db.add(redemption)
            db.flush()
    except IntegrityError:
        return db.execute(
            select(CouponRedemption).where(
                CouponRedemption.coupon_id == coupon.id,
                CouponRedemption.order_id == order.id,
            )
        ).scalar_one()
    locked.used_count = int(locked.used_count or 0) + 1
    if exceeded:
        logger.warning(
            "Coupon %s use by order %s exceeds %s; counted and flagged for review",
            locked.code,
            order.id,
            ", ".join(exceeded),
        )
        AuditService.log(
            db,
            actor_user_id=None,
            billing_account_id=order.billing_account_id,
            action="billing.coupon_limit_exceeded",
            resource_type="coupon",
            resource_id=str(locked.id),
            metadata={
                "code": locked.code,
                "order_id": order.id,
                "invoice_id": invoice_id,
                "limits": exceeded,
                "used_count": int(locked.used_count),
                "max_uses": locked.max_uses,
                "max_uses_per_account": locked.max_uses_per_account,
                "review": True,
            },
        )
    db.flush()
    return redemption


def _limits_exceeded(
    db: Session, coupon: Coupon, billing_account_id: Optional[int]
) -> list[str]:
    """Limits one more use would break, read under the coupon row lock."""
    exceeded: list[str] = []
    if coupon.max_uses is not None and int(coupon.used_count or 0) >= int(coupon.max_uses):
        exceeded.append("max_uses")
    if (
        billing_account_id is not None
        and coupon.max_uses_per_account is not None
        and account_redemptions(db, coupon.id, billing_account_id)
        >= int(coupon.max_uses_per_account)
    ):
        exceeded.append("max_uses_per_account")
    return exceeded


def release_coupon_usage(
    db: Session, *, order: Order, actor_user_id: Optional[int] = None, reason: str
) -> bool:
    """Give back the coupon use counted for an order that kept no money. Idempotent.

    Callers release only when no money is held for the order (unpaid, or
    every payment refunded); while money waits for a refund review the use
    stays counted. Returns True when a use was released.
    """
    from app.services.billing_locks import BillingLocks

    coupon_ids = db.scalars(
        select(CouponRedemption.coupon_id).where(CouponRedemption.order_id == order.id)
    ).all()
    released = False
    for coupon_id in sorted(set(coupon_ids)):
        locked = BillingLocks.coupon(db, int(coupon_id))
        redemption = db.execute(
            select(CouponRedemption).where(
                CouponRedemption.coupon_id == coupon_id,
                CouponRedemption.order_id == order.id,
            )
        ).scalar_one_or_none()
        if redemption is None:
            continue
        db.delete(redemption)
        locked.used_count = max(0, int(locked.used_count or 0) - 1)
        AuditService.log(
            db,
            actor_user_id=actor_user_id,
            billing_account_id=order.billing_account_id,
            action="billing.coupon_usage_released",
            resource_type="coupon",
            resource_id=str(locked.id),
            metadata={"code": locked.code, "order_id": order.id, "reason": reason},
        )
        released = True
    db.flush()
    return released
