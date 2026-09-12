"""GDPR export service tests."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.dao.commerce_dao import BillingAccountDAO
from app.models.user import User
from app.services.gdpr_service import GdprService


def _make_user(db: Session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_export_user_data_includes_core_fields(db_session: Session):
    user = _make_user(db_session, "gdpr-user")
    BillingAccountDAO.ensure_client_account(db_session, user.id)
    payload = GdprService.export_user_data(db_session, user.id)
    assert payload["user"]["id"] == user.id
    assert payload["user"]["username"] == user.username
    assert "exported_at" in payload
    assert "orders" in payload
    assert "invoices" in payload
