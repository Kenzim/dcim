"""Commerce order persistence and status history."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.dao.sequence_dao import SequenceDAO
from app.models.commerce_order import (
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
)


class OrderDAO:
    _SEQUENCE_NAME = "orders"
    _FIRST_NUMBER = 100001

    @staticmethod
    def allocate_order_number(db: Session) -> int:
        return SequenceDAO.allocate(
            db,
            OrderDAO._SEQUENCE_NAME,
            table_model=Order,
            column=Order.order_number,
            first_number=OrderDAO._FIRST_NUMBER,
        )

    @staticmethod
    def create(db: Session, **fields: Any) -> Order:
        if "order_number" not in fields:
            fields = dict(fields)
            fields["order_number"] = OrderDAO.allocate_order_number(db)
        row = Order(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def get(db: Session, order_id: int) -> Optional[Order]:
        return db.get(Order, order_id)

    @staticmethod
    def get_for_update(db: Session, order_id: int) -> Optional[Order]:
        return db.execute(
            select(Order).where(Order.id == order_id).with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def get_by_number(db: Session, order_number: int) -> Optional[Order]:
        return db.execute(
            select(Order).where(Order.order_number == order_number)
        ).scalar_one_or_none()

    @staticmethod
    def list_for_account(
        db: Session,
        billing_account_id: int,
        *,
        status: Optional[OrderStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Order]:
        stmt = (
            select(Order)
            .options(joinedload(Order.items))
            .where(Order.billing_account_id == billing_account_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
        )
        if status is not None:
            stmt = stmt.where(Order.status == status)
        return list(db.execute(stmt.offset(offset).limit(limit)).unique().scalars())

    @staticmethod
    def list_admin(
        db: Session,
        *,
        status: Optional[OrderStatus] = None,
        q: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Order]:
        stmt = select(Order).order_by(Order.created_at.desc(), Order.id.desc())
        if status is not None:
            stmt = stmt.where(Order.status == status)
        if q:
            needle = q.strip()
            if needle.isdigit():
                stmt = stmt.where(
                    or_(
                        Order.order_number == int(needle),
                        Order.id == int(needle),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        Order.coupon_code.ilike(f"%{needle}%"),
                        Order.notes.ilike(f"%{needle}%"),
                    )
                )
        return list(db.execute(stmt.offset(offset).limit(limit)).scalars())


class OrderItemDAO:
    @staticmethod
    def create(db: Session, **fields: Any) -> OrderItem:
        row = OrderItem(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def get(db: Session, item_id: int) -> Optional[OrderItem]:
        return db.get(OrderItem, item_id)

    @staticmethod
    def list_for_order(db: Session, order_id: int) -> list[OrderItem]:
        return list(
            db.execute(
                select(OrderItem)
                .where(OrderItem.order_id == order_id)
                .order_by(OrderItem.id)
            ).scalars()
        )


class OrderStatusHistoryDAO:
    @staticmethod
    def append(
        db: Session,
        *,
        order_id: int,
        from_status: Optional[OrderStatus],
        to_status: OrderStatus,
        actor_user_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> OrderStatusHistory:
        row = OrderStatusHistory(
            order_id=order_id,
            from_status=from_status,
            to_status=to_status,
            actor_user_id=actor_user_id,
            note=note,
        )
        db.add(row)
        db.flush()
        return row
