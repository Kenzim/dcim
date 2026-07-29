"""Billing power-control gating for suspended/terminated services."""
import pytest

from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.user_dao import UserDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus


def _integration_key(db_session):
    integration = BillingIntegrationDAO.create(
        db_session, name="whmcs-power", integration_type="whmcs"
    )
    return integration, integration.plaintext_api_key


def _service_for(db_session, integration, status):
    billing_user = UserDAO.create(
        db_session,
        username="poweruser",
        email="power@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-power-1",
        external_username="poweruser",
        external_email="power@example.com",
    )
    return ServiceDAO.create_vm(
        db_session,
        name="svc-power",
        owner_user_id=billing_user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=status,
    )


@pytest.mark.parametrize("action", ["on", "reboot", "reset"])
def test_power_blocked_for_suspended_service(client, db_session, action):
    integration, key = _integration_key(db_session)
    service = _service_for(db_session, integration, ServiceStatus.SUSPENDED)

    resp = client.post(
        f"/api/billing/services/{service.id}/power",
        headers={"Authorization": f"Bearer {key}"},
        json={"action": action},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("action", ["on", "reboot", "reset"])
def test_power_blocked_for_terminated_service(client, db_session, action):
    integration, key = _integration_key(db_session)
    service = _service_for(db_session, integration, ServiceStatus.TERMINATED)

    resp = client.post(
        f"/api/billing/services/{service.id}/power",
        headers={"Authorization": f"Bearer {key}"},
        json={"action": action},
    )
    assert resp.status_code == 403, resp.text
