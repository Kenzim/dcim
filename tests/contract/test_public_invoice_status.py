"""The ``status`` field of invoices on the frozen client commerce and reseller APIs.

Billing phase 1 adds invoice statuses; ``/api/client/commerce/*`` and
``/api/reseller/*`` keep showing only the seven statuses they always have, and
carry the real value in ``status_detail`` (docs/billing-phase1-design.md 12,
question 4).
"""

import pytest

from app.models.reseller import InvoiceStatus
from app.services.invoice_service import PUBLIC_INVOICE_STATUSES, public_invoice_status

TODAYS_STATUSES = {"draft", "open", "paid", "void", "failed", "pending_action", "overdue"}

EXPECTED = {
    "draft": "draft",
    "open": "open",
    "paid": "paid",
    "void": "void",
    "failed": "failed",
    "pending_action": "pending_action",
    "overdue": "overdue",
    "partially_paid": "open",
    "collections": "overdue",
    "refunded": "paid",
}


def test_public_statuses_are_todays_seven():
    assert set(PUBLIC_INVOICE_STATUSES) == TODAYS_STATUSES


def test_mapping_of_every_internal_status_is_pinned():
    assert {status.value for status in InvoiceStatus} == set(EXPECTED)
    assert {status.value: public_invoice_status(status) for status in InvoiceStatus} == EXPECTED


@pytest.mark.parametrize("status", list(InvoiceStatus))
def test_no_status_maps_outside_todays_values(status):
    assert public_invoice_status(status) in TODAYS_STATUSES
    assert public_invoice_status(status.value) == public_invoice_status(status)


def test_refunded_is_paid_never_failed():
    assert public_invoice_status(InvoiceStatus.REFUNDED) == "paid"
    assert public_invoice_status("refunded") != "failed"


def test_unknown_status_is_refused():
    with pytest.raises(ValueError):
        public_invoice_status("written_off")


def test_serialisers_show_public_status_and_real_detail(db_session):
    from app.api.reseller import _invoice_payload
    from app.models.reseller import Invoice, InvoicePurpose
    from app.services.invoice_service import InvoiceService

    invoice = Invoice(
        invoice_number=777001,
        purpose=InvoicePurpose.DEPLOY_CHARGE,
        status=InvoiceStatus.REFUNDED,
        amount_cents=1000,
        currency="USD",
    )
    db_session.add(invoice)
    db_session.flush()
    for payload in (InvoiceService.serialize(db_session, invoice), _invoice_payload(invoice)):
        assert payload["status"] == "paid"
        assert payload["status_detail"] == "refunded"
