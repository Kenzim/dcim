"""Invoice PDF helper tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.dao.commerce_dao import BillingAccountDAO
from app.models.user import User
from app.services.invoice_pdf_service import InvoicePdfService, _invoice_profile_lines
from app.services.invoice_service import InvoiceService
from app.models.reseller import InvoicePurpose


def _make_user(db: Session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_invoice_profile_lines_empty_without_account(db_session: Session):
    invoice = MagicMock()
    invoice.billing_account_id = None
    assert _invoice_profile_lines(db_session, invoice) == []


def test_render_pdf_bytes_without_reportlab(db_session: Session):
    user = _make_user(db_session, "pdf-user")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    invoice = InvoiceService.create(
        db_session,
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        amount_cents=500,
        description="pdf test",
    )
    db_session.commit()
    with patch.dict("sys.modules", {"reportlab": None, "reportlab.lib": None}):
        with pytest.raises(RuntimeError, match="reportlab"):
            InvoicePdfService.render_pdf_bytes(db_session, invoice)
