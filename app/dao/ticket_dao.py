"""Support ticket persistence."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.dao.sequence_dao import SequenceDAO
from app.models.support_ticket import (
    Ticket,
    TicketDepartment,
    TicketMessage,
    TicketPriority,
    TicketStatus,
)


class TicketDepartmentDAO:
    @staticmethod
    def get(db: Session, department_id: int) -> Optional[TicketDepartment]:
        return db.get(TicketDepartment, department_id)

    @staticmethod
    def get_by_code(db: Session, code: str) -> Optional[TicketDepartment]:
        return db.execute(
            select(TicketDepartment).where(TicketDepartment.code == code.strip())
        ).scalar_one_or_none()

    @staticmethod
    def list_enabled(db: Session) -> list[TicketDepartment]:
        return list(
            db.execute(
                select(TicketDepartment)
                .where(TicketDepartment.enabled.is_(True))
                .order_by(TicketDepartment.sort_order, TicketDepartment.name)
            ).scalars()
        )

    @staticmethod
    def list_admin(db: Session) -> list[TicketDepartment]:
        return list(
            db.execute(
                select(TicketDepartment).order_by(
                    TicketDepartment.sort_order, TicketDepartment.name
                )
            ).scalars()
        )

    @staticmethod
    def create(db: Session, **fields: Any) -> TicketDepartment:
        row = TicketDepartment(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(
        db: Session, row: TicketDepartment, **fields: Any
    ) -> TicketDepartment:
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row


class TicketDAO:
    _SEQUENCE_NAME = "tickets"
    _FIRST_NUMBER = 100001

    @staticmethod
    def allocate_ticket_number(db: Session) -> int:
        return SequenceDAO.allocate(
            db,
            TicketDAO._SEQUENCE_NAME,
            table_model=Ticket,
            column=Ticket.ticket_number,
            first_number=TicketDAO._FIRST_NUMBER,
        )

    @staticmethod
    def create(db: Session, **fields: Any) -> Ticket:
        if "ticket_number" not in fields:
            fields = dict(fields)
            fields["ticket_number"] = TicketDAO.allocate_ticket_number(db)
        row = Ticket(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def get(db: Session, ticket_id: int) -> Optional[Ticket]:
        return db.get(Ticket, ticket_id)

    @staticmethod
    def get_by_number(db: Session, ticket_number: int) -> Optional[Ticket]:
        return db.execute(
            select(Ticket).where(Ticket.ticket_number == ticket_number)
        ).scalar_one_or_none()

    @staticmethod
    def list_for_account(
        db: Session,
        billing_account_id: int,
        *,
        status: Optional[TicketStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ticket]:
        stmt = (
            select(Ticket)
            .options(joinedload(Ticket.department))
            .where(Ticket.billing_account_id == billing_account_id)
            .order_by(Ticket.updated_at.desc(), Ticket.id.desc())
        )
        if status is not None:
            stmt = stmt.where(Ticket.status == status)
        return list(db.execute(stmt.offset(offset).limit(limit)).unique().scalars())

    @staticmethod
    def list_admin_queue(
        db: Session,
        *,
        status: Optional[TicketStatus] = None,
        department_id: Optional[int] = None,
        assigned_admin_id: Optional[int] = None,
        q: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ticket]:
        stmt = (
            select(Ticket)
            .options(
                joinedload(Ticket.department),
                joinedload(Ticket.user),
            )
            .order_by(Ticket.updated_at.desc(), Ticket.id.desc())
        )
        if status is not None:
            stmt = stmt.where(Ticket.status == status)
        if department_id is not None:
            stmt = stmt.where(Ticket.department_id == department_id)
        if assigned_admin_id is not None:
            stmt = stmt.where(Ticket.assigned_admin_id == assigned_admin_id)
        if q:
            needle = q.strip()
            if needle.isdigit():
                stmt = stmt.where(
                    or_(
                        Ticket.ticket_number == int(needle),
                        Ticket.id == int(needle),
                    )
                )
            else:
                stmt = stmt.where(Ticket.subject.ilike(f"%{needle}%"))
        return list(db.execute(stmt.offset(offset).limit(limit)).unique().scalars())


class TicketMessageDAO:
    @staticmethod
    def create(db: Session, **fields: Any) -> TicketMessage:
        row = TicketMessage(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def get(db: Session, message_id: int) -> Optional[TicketMessage]:
        return db.get(TicketMessage, message_id)

    @staticmethod
    def list_for_ticket(db: Session, ticket_id: int) -> list[TicketMessage]:
        return list(
            db.execute(
                select(TicketMessage)
                .where(TicketMessage.ticket_id == ticket_id)
                .order_by(TicketMessage.created_at, TicketMessage.id)
            ).scalars()
        )
