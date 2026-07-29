"""
Registering a new billing service auto-provisions client portal access
(non-admin User with a billing identity) for the external user, with a
blank password.
"""
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.user_dao import UserDAO


def test_register_service_auto_provisions_client_portal_access(client, db_session):
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-register", integration_type="whmcs")
    api_key = integration.plaintext_api_key

    location = LocationDAO.create(db_session, name="loc-register-1")
    server = ServerDAO.create(
        db_session,
        name="srv-register-1",
        server_ip="10.10.10.10",
        plugin_name="ipmi",
        plugin_config={},
        location_id=location.id,
    )

    resp = client.post(
        "/api/billing/register-service",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "server_id": server.id,
            "external_service_id": "whmcs-svc-1",
            "external_user_id": "whmcs-user-1",
            "external_username": "newbillinguser",
            "external_email": "newbillinguser@example.com",
        },
    )
    assert resp.status_code == 201, resp.text
    # In the collapsed identity model, the response's external_user_id is
    # the owning Rackflow User's id (not the WHMCS-side string).
    owner_user_id = resp.json()["external_user_id"]
    assert owner_user_id is not None

    # A non-admin User with a linked billing identity must have been
    # auto-provisioned immediately — no separate "enable portal access"
    # admin action required.
    user = UserDAO.get_by_id(db_session, owner_user_id)
    assert user is not None
    assert user.billing_integration_id == integration.id
    assert user.external_user_id == "whmcs-user-1"
    assert user.is_admin is False
    assert user.has_password is False
    assert user.password is None
