from app.dao.user_dao import UserDAO
from app.dao.service_dao import ServiceDAO
from app.models.billing_integration import BillingIntegration
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.user import User


def _login(client, username: str, password: str) -> str:
    resp = client.post("/api/users/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()["token"]


def test_billing_owned_service_reports_owner_and_billing_identity(client, db_session, test_admin_user):
    """In the collapsed model, billing ownership is `owner_user_id` set at
    creation time — the (now-removed) external-user-links backfill flow no
    longer exists."""
    integration = BillingIntegration(
        name="whmcs-main",
        integration_type="whmcs",
        api_key="k-test-1",
        enabled=True,
        config={},
    )
    db_session.add(integration)
    db_session.commit()
    db_session.refresh(integration)

    billing_user = UserDAO.create(
        db_session,
        username="client1",
        email="client1@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-001",
        external_username="extuser",
        external_email="ext@example.com",
    )

    service = ServiceDAO.create_vm(
        db_session,
        name="svc-billing-owner",
        owner_user_id=billing_user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.PENDING,
    )
    assert service.owner_user_id == billing_user.id

    admin_token = _login(client, "admin", "adminpassword123")
    service_get = client.get(
        f"/api/admin/services/{service.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert service_get.status_code == 200
    body = service_get.json()
    assert body["owner_user_id"] == billing_user.id
    assert body["owner_username"] == "client1"
    assert body["external_user_id"] == billing_user.id
    assert body["external_username"] == "extuser"


def test_client_user_can_list_owned_services(client, db_session, test_admin_user):
    client_user = User(username="client2", email="client2@example.com", is_admin=False)
    client_user.set_password("secret123")
    db_session.add(client_user)
    db_session.commit()
    db_session.refresh(client_user)

    service = ServiceDAO.create_vm(
        db_session,
        name="owned-service",
        owner_user_id=client_user.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
    )

    token = _login(client, "client2", "secret123")
    resp = client.get("/api/services/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    ids = {row["id"] for row in rows}
    assert service.id in ids
