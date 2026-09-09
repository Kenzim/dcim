"""Admin support ticket queue and department management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.ticket_dao import TicketDAO, TicketDepartmentDAO, TicketMessageDAO
from app.models.support_ticket import Ticket, TicketStatus
from app.services.ticket_service import TicketService

router = APIRouter(
    prefix="/admin/support",
    tags=["commerce-support-admin"],
    dependencies=[Depends(require_admin)],
)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TicketReplyBody(RequestModel):
    body_text: str = Field(min_length=1, max_length=65535)
    is_staff_note: StrictBool = False


class TicketPatchBody(RequestModel):
    status: Optional[TicketStatus] = None
    assigned_admin_id: Optional[StrictInt] = Field(default=None, gt=0)


def _serialize_department(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "code": row.code,
        "description": row.description,
        "sort_order": row.sort_order,
        "enabled": row.enabled,
    }


def _serialize_message(msg) -> dict:
    return {
        "id": msg.id,
        "author_user_id": msg.author_user_id,
        "body_text": msg.body_text,
        "is_staff_note": msg.is_staff_note,
        "created_at": msg.created_at,
    }


def _serialize_ticket(ticket: Ticket, *, include_messages: bool = False) -> dict:
    payload = {
        "id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "billing_account_id": ticket.billing_account_id,
        "user_id": ticket.user_id,
        "department_id": ticket.department_id,
        "department_name": ticket.department.name if ticket.department else None,
        "service_id": ticket.service_id,
        "subject": ticket.subject,
        "status": ticket.status.value,
        "priority": ticket.priority.value,
        "assigned_admin_id": ticket.assigned_admin_id,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "closed_at": ticket.closed_at,
        "username": ticket.user.username if ticket.user else None,
    }
    if include_messages:
        payload["messages"] = [_serialize_message(msg) for msg in ticket.messages]
    return payload


@router.get("/departments")
def list_departments(db: Session = Depends(get_db)):
    rows = TicketDepartmentDAO.list_admin(db)
    return [_serialize_department(row) for row in rows]


@router.get("/tickets")
def list_tickets(
    status_filter: Optional[TicketStatus] = Query(default=None, alias="status"),
    department_id: Optional[int] = None,
    assigned_admin_id: Optional[int] = None,
    q: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    rows = TicketService.list_admin_queue(
        db,
        status=status_filter,
        department_id=department_id,
        assigned_admin_id=assigned_admin_id,
        q=q,
        limit=limit,
        offset=offset,
    )
    return [_serialize_ticket(row) for row in rows]


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ticket.messages = TicketMessageDAO.list_for_ticket(db, ticket_id)
    return _serialize_ticket(ticket, include_messages=True)


@router.post("/tickets/{ticket_id}/reply", status_code=status.HTTP_201_CREATED)
def reply_to_ticket(
    ticket_id: int,
    body: TicketReplyBody,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ticket = TicketDAO.get(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    try:
        message = TicketService.add_message(
            db,
            ticket_id=ticket_id,
            author_user_id=auth.get("user_id"),
            body_text=body.body_text,
            is_staff_note=body.is_staff_note,
            reopen_for_customer=not body.is_staff_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _serialize_message(message)


@router.patch("/tickets/{ticket_id}")
def patch_ticket(
    ticket_id: int,
    body: TicketPatchBody,
    db: Session = Depends(get_db),
):
    ticket = TicketDAO.get(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    data = body.model_dump(exclude_unset=True)
    if "status" in data:
        ticket.status = data["status"]
        if data["status"] == TicketStatus.CLOSED:
            ticket.closed_at = datetime.now(timezone.utc)
        elif ticket.closed_at is not None:
            ticket.closed_at = None
    if "assigned_admin_id" in data:
        ticket.assigned_admin_id = data["assigned_admin_id"]
    ticket.updated_at = datetime.now(timezone.utc)
    db.commit()
    return _serialize_ticket(ticket)
