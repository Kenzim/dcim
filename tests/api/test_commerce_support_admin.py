"""Admin support ticket queue endpoints."""
from __future__ import annotations

from app.dao.commerce_dao import BillingAccountDAO
from app.models.support_ticket import TicketDepartment, TicketStatus
from app.models.user import User
from app.services.ticket_service import TicketService


def _login_admin(client) -> dict:
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_support_admin_queue_and_ticket_actions(client, db_session, test_admin_user):
    headers = _login_admin(client)
    empty = client.get("/api/admin/support/departments", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == []

    user = User(username="ticket-owner", email="ticket-owner@example.com", is_admin=False)
    user.set_password("password123")
    db_session.add(user)
    db_session.flush()
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    dept = TicketDepartment(
        name="Billing", code="billing", description="bills", sort_order=1, enabled=True
    )
    db_session.add(dept)
    db_session.flush()
    ticket = TicketService.create_ticket(
        db_session,
        account=account,
        user_id=user.id,
        department_id=dept.id,
        subject="Help",
        body_text="Need help",
    )
    db_session.commit()

    depts = client.get("/api/admin/support/departments", headers=headers)
    assert depts.status_code == 200
    assert any(row["code"] == "billing" for row in depts.json())

    listed = client.get("/api/admin/support/tickets?q=Help", headers=headers)
    assert listed.status_code == 200
    assert any(row["id"] == ticket.id for row in listed.json())

    missing = client.get("/api/admin/support/tickets/999999", headers=headers)
    assert missing.status_code == 404

    detail = client.get(f"/api/admin/support/tickets/{ticket.id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["subject"] == "Help"
    assert detail.json()["messages"]

    reply_missing = client.post(
        "/api/admin/support/tickets/999999/reply",
        headers=headers,
        json={"body_text": "hello"},
    )
    assert reply_missing.status_code == 404

    replied = client.post(
        f"/api/admin/support/tickets/{ticket.id}/reply",
        headers=headers,
        json={"body_text": "staff reply", "is_staff_note": False},
    )
    assert replied.status_code == 201, replied.text

    patch_missing = client.patch(
        "/api/admin/support/tickets/999999",
        headers=headers,
        json={"status": "closed"},
    )
    assert patch_missing.status_code == 404

    closed = client.patch(
        f"/api/admin/support/tickets/{ticket.id}",
        headers=headers,
        json={"status": TicketStatus.CLOSED.value, "assigned_admin_id": test_admin_user.id},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    assert closed.json()["assigned_admin_id"] == test_admin_user.id

    reopened = client.patch(
        f"/api/admin/support/tickets/{ticket.id}",
        headers=headers,
        json={"status": TicketStatus.OPEN.value},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"
    assert reopened.json()["closed_at"] is None
