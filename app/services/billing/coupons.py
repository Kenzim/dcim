"""Coupon rules that depend on stored usage, and usage counting at payment."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.commerce_coupon_tax import Coupon, CouponDuration, CouponRedemption
from app.models.commerce_order import Order

logger = logging.getLogger(__name__)


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
    db.flush()
    return redemption
