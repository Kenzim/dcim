"""Support ticket creation and messaging."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.dao.ticket_dao import TicketDAO, TicketDepartmentDAO, TicketMessageDAO
from app.models.commerce_account import BillingAccount
from app.models.support_ticket import Ticket, TicketMessage, TicketPriority, TicketStatus
from app.services.commerce_webhook_service import CommerceWebhookService


class TicketService:
    @staticmethod
    def create_ticket(
        db: Session,
        *,
        account: BillingAccount,
        user_id: int,
        department_id: int,
        subject: str,
        body_text: str,
        priority: TicketPriority = TicketPriority.MEDIUM,
        service_id: Optional[int] = None,
    ) -> Ticket:
        department = TicketDepartmentDAO.get(db, department_id)
        if department is None or not department.enabled:
            raise ValueError("Support department is unavailable")
        ticket = TicketDAO.create(
            db,
            billing_account_id=account.id,
            user_id=user_id,
            department_id=department_id,
            service_id=service_id,
            subject=subject.strip()[:512],
            status=TicketStatus.OPEN,
            priority=priority,
        )
        TicketMessageDAO.create(
            db,
            ticket_id=ticket.id,
            author_user_id=user_id,
            body_text=body_text.strip(),
            is_staff_note=False,
        )
        try:
            CommerceWebhookService.enqueue(
                db,
                "ticket.created",
                {
                    "ticket_id": ticket.id,
                    "ticket_number": ticket.ticket_number,
                    "billing_account_id": account.id,
                    "department_id": department_id,
                    "subject": ticket.subject,
                },
            )
        except Exception:
            pass
        return ticket

    @staticmethod
    def add_message(
        db: Session,
        *,
        ticket_id: int,
        author_user_id: Optional[int],
        body_text: str,
        is_staff_note: bool = False,
        reopen_for_customer: bool = True,
    ) -> TicketMessage:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            raise ValueError("Ticket not found")
        message = TicketMessageDAO.create(
            db,
            ticket_id=ticket_id,
            author_user_id=author_user_id,
            body_text=body_text.strip(),
            is_staff_note=is_staff_note,
        )
        if is_staff_note:
            ticket.updated_at = datetime.now(timezone.utc)
        elif reopen_for_customer:
            if ticket.status == TicketStatus.CLOSED:
                ticket.status = TicketStatus.OPEN
                ticket.closed_at = None
            elif ticket.status in {TicketStatus.ANSWERED, TicketStatus.ON_HOLD}:
                ticket.status = TicketStatus.CUSTOMER_REPLY
            ticket.updated_at = datetime.now(timezone.utc)
        else:
            ticket.status = TicketStatus.ANSWERED
            ticket.updated_at = datetime.now(timezone.utc)
        db.flush()
        return message

    @staticmethod
    def list_for_account(
        db: Session,
        billing_account_id: int,
        *,
        status: Optional[TicketStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ticket]:
        return TicketDAO.list_for_account(
            db,
            billing_account_id,
            status=status,
            limit=limit,
            offset=offset,
        )

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
        return TicketDAO.list_admin_queue(
            db,
            status=status,
            department_id=department_id,
            assigned_admin_id=assigned_admin_id,
            q=q,
            limit=limit,
            offset=offset,
        )
