"""Staff unsuspend and terminate through the admin status route and the WHMCS API (P2-L review fixes M5, L4)."""

from __future__ import annotations

from datetime import timedelta

from app.models.commerce_audit import UserAuditEvent
from app.models.reseller import ServiceBilling, ServiceBillingStatus
from app.models.service import Service, ServiceStatus
from app.services.alerting import billing_integrity
from tests.api.test_billing_deep_coverage import _bm_owned
from tests.api.test_runner_proxy_lifecycle import _admin_headers
from tests.services.p2l_support import (
    NOW,
    make_account,
    make_billing,
    make_cycle_invoice,
    make_reseller,
    make_service,
)

RELEASED = "billing.nonpayment_suspension_released_by_staff"
PARAMS = {"max_rows": 10}


def _suspended_retail(db, account=None, service=None):
    account = account or make_account(db)
    service = service or make_service(db, account, status=ServiceStatus.SUSPENDED)
    service.status = ServiceStatus.SUSPENDED
    db.commit()
    billing = make_billing(db, account, service, status=ServiceBillingStatus.SUSPENDED_NONPAYMENT)
    invoice = make_cycle_invoice(db, billing, due_at=NOW - timedelta(days=20))
    return billing, invoice


def _billing_status(db, billing):
    db.expire_all()
    return db.get(ServiceBilling, billing.id).status


def test_staff_unsuspend_through_the_status_route_releases_a_retail_billing(client, db_session, test_admin_user):
    billing, invoice = _suspended_retail(db_session)
    response = client.put(
        f"/api/admin/services/{billing.service_id}/status", headers=_admin_headers(client, test_admin_user),
        json={"status": "active"},
    )
    assert response.status_code == 200, response.text
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.ACTIVE
    assert _billing_status(db_session, billing) == ServiceBillingStatus.ACTIVE
    audit = db_session.query(UserAuditEvent).filter_by(action=RELEASED).one()
    assert audit.actor_user_id == test_admin_user.id
    assert audit.event_metadata == {
        "billing_id": billing.id, "old_status": "suspended_nonpayment", "new_status": "active",
        "unpaid_invoice_ids": [invoice.id],
    }
    assert billing_integrity.nonpayment_suspension_failed(db_session, PARAMS, NOW) == []


def test_staff_unsuspend_leaves_a_reseller_billing_alone(client, db_session, test_admin_user):
    reseller = make_reseller(db_session)
    service = make_service(db_session, None, status=ServiceStatus.SUSPENDED)
    billing = make_billing(db_session, None, service, reseller_id=reseller.id,
                           status=ServiceBillingStatus.SUSPENDED_NONPAYMENT)
    response = client.put(
        f"/api/admin/services/{service.id}/status", headers=_admin_headers(client, test_admin_user),
        json={"status": "active"},
    )
    assert response.status_code == 200, response.text
    assert _billing_status(db_session, billing) == ServiceBillingStatus.SUSPENDED_NONPAYMENT
    assert db_session.query(UserAuditEvent).filter_by(action=RELEASED).count() == 0


def test_whmcs_unsuspend_releases_a_retail_billing(client, db_session):
    from app.dao.commerce_dao import BillingAccountDAO

    _, headers, service, _ = _bm_owned(db_session, "release-whmcs")
    account = BillingAccountDAO.ensure_client_account(db_session, service.owner_user_id)
    billing, _ = _suspended_retail(db_session, account, service)
    response = client.post(f"/api/billing/services/{service.id}/unsuspend", headers=headers, json={"reason": "paid"})
    assert response.status_code == 200, response.text
    assert db_session.get(Service, service.id).status == ServiceStatus.ACTIVE
    assert _billing_status(db_session, billing) == ServiceBillingStatus.ACTIVE
    assert db_session.query(UserAuditEvent).filter_by(action=RELEASED).one().actor_user_id is None


# ---------------------------------------------------------------- L4


def test_staff_terminate_through_the_status_route_ends_a_retail_nonpayment_billing(client, db_session, test_admin_user):
    from app.models.billing_jobs import UnpaidInvoiceAction
    from app.models.reseller import Invoice, InvoiceStatus
    from tests.services.p2l_support import set_policy

    set_policy(db_session, suspend_after_days=5, terminate_after_days=30,
               unpaid_invoice_action_on_terminate=UnpaidInvoiceAction.CANCEL)
    billing, invoice = _suspended_retail(db_session)
    response = client.put(
        f"/api/admin/services/{billing.service_id}/status", headers=_admin_headers(client, test_admin_user),
        json={"status": "terminated"},
    )
    assert response.status_code == 200, response.text
    db_session.expire_all()
    row = db_session.get(ServiceBilling, billing.id)
    assert row.status == ServiceBillingStatus.CANCELLED
    assert row.next_charge_at is None and row.last_failure_code == "nonpayment_terminated"
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.VOID
    audit = db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_billing_ended_by_staff").one()
    assert audit.event_metadata["cancelled_invoice_ids"] == [invoice.id]


def test_staff_terminate_leaves_an_active_billing_to_the_recurring_pass(client, db_session, test_admin_user):
    account = make_account(db_session)
    service = make_service(db_session, account)
    billing = make_billing(db_session, account, service)
    response = client.put(
        f"/api/admin/services/{service.id}/status", headers=_admin_headers(client, test_admin_user),
        json={"status": "terminated"},
    )
    assert response.status_code == 200, response.text
    assert _billing_status(db_session, billing) == ServiceBillingStatus.ACTIVE
