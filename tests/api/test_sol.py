"""SOL API: profiles, redeem, tickets, popups, send, status flag."""
from unittest.mock import AsyncMock

from app.core.client_permissions import PermissionKey
from app.core.config import settings
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceStatus
from app.services.sol.ticket_service import mint_launch_ticket


def _login_admin(client, test_admin_user):
    r = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _login_user(client, test_user, password="testpassword123"):
    r = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": password},
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _server(db_session, *, profile="ipmi_sol", name="sol-srv"):
    location = LocationDAO.create(db_session, name=f"loc-{name}")
    return ServerDAO.create(
        db_session,
        name=name,
        server_ip="10.16.251.138",
        plugin_name="ipmi",
        plugin_config={"hostname": "10.16.251.138", "username": "admin", "password": "secret"},
        location_id=location.id,
        sol_profile=profile,
    )


def _bm_service(db_session, server, *, owner_user_id=None, permission_overrides=None, billing=False):
    kwargs = {}
    if billing:
        kwargs["provisioning_source"] = ProvisioningSource.BILLING
        kwargs["status"] = ServiceStatus.ACTIVE
    else:
        kwargs["provisioning_source"] = ProvisioningSource.INTERNAL
        kwargs["status"] = ServiceStatus.ACTIVE
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="bm-sol",
        server_id=server.id,
        owner_user_id=owner_user_id,
        **kwargs,
    )
    if permission_overrides is not None:
        service.permission_overrides = permission_overrides
        ServiceDAO.update(db_session, service)
    return service


def test_admin_lists_sol_profiles(client, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    resp = client.get("/api/sol/profiles", headers=headers)
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert "ipmi_sol" in ids


def test_create_server_rejects_unknown_sol_profile(client, test_admin_user, db_session):
    headers = _login_admin(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-bad-sol")
    resp = client.post(
        "/api/servers/",
        headers=headers,
        json={
            "name": "bad-sol-profile",
            "server_ip": "10.9.9.8",
            "location_id": location.id,
            "plugin_name": "ipmi",
            "plugin_config": {"hostname": "10.9.9.8", "username": "admin", "password": "x"},
            "sol_profile": "redfish",
        },
    )
    assert resp.status_code == 400, resp.text


def test_redeem_invalid_token_is_400(client):
    resp = client.post("/api/sol/redeem", json={"token": "nope"})
    assert resp.status_code == 400, resp.text


def test_redeem_without_profile_is_409(client, db_session):
    server = _server(db_session, profile=None, name="sol-off")
    token = mint_launch_ticket(server.id)
    resp = client.post("/api/sol/redeem", json={"token": token})
    assert resp.status_code == 409, resp.text


def test_redeem_mints_ws_session(client, db_session):
    server = _server(db_session)
    token = mint_launch_ticket(server.id)
    resp = client.post("/api/sol/redeem", json={"token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ws_path"] == "/api/sol/ws"
    assert body["profile"] == "ipmi_sol"
    assert "ws_token" in body
    assert client.post("/api/sol/redeem", json={"token": token}).status_code == 400


def test_admin_server_sol_popup_without_profile_errors(client, test_admin_user, db_session):
    headers = _login_admin(client, test_admin_user)
    server = _server(db_session, profile=None, name="sol-popup-off")
    resp = client.get(f"/api/servers/{server.id}/sol-popup", headers=headers, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/sol?e=")


def test_admin_server_sol_popup_with_profile(client, test_admin_user, db_session):
    headers = _login_admin(client, test_admin_user)
    server = _server(db_session, name="sol-popup-on")
    resp = client.get(f"/api/servers/{server.id}/sol-popup", headers=headers, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/sol?t=")


def test_client_sol_popup_denied_without_permission(client, db_session, test_user):
    headers = _login_user(client, test_user)
    server = _server(db_session, name="sol-client-deny")
    service = _bm_service(
        db_session,
        server,
        owner_user_id=test_user.id,
        permission_overrides={PermissionKey.BMS_SOL: False},
    )
    resp = client.get(
        f"/api/client/services/{service.id}/sol-popup", headers=headers, follow_redirects=False
    )
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/sol?e=")


def test_client_sol_popup_when_permitted(client, db_session, test_user):
    headers = _login_user(client, test_user)
    server = _server(db_session, name="sol-client-ok")
    service = _bm_service(db_session, server, owner_user_id=test_user.id)
    resp = client.get(
        f"/api/client/services/{service.id}/sol-popup", headers=headers, follow_redirects=False
    )
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/sol?t=")


def test_admin_sol_send_empty_is_400(client, test_admin_user, db_session):
    headers = _login_admin(client, test_admin_user)
    server = _server(db_session, name="sol-send-empty")
    resp = client.post(
        f"/api/servers/{server.id}/sol/send",
        headers=headers,
        json={"data": ""},
    )
    assert resp.status_code == 400, resp.text


def test_admin_sol_send_injects(client, test_admin_user, db_session, monkeypatch):
    headers = _login_admin(client, test_admin_user)
    server = _server(db_session, name="sol-send-ok")
    inject = AsyncMock(return_value=b"ok")
    monkeypatch.setattr("app.api.sol.inject_bytes", inject)
    resp = client.post(
        f"/api/servers/{server.id}/sol/send",
        headers=headers,
        json={"data": "root\n", "wait_ms": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["written"] == 5
    assert body["output"] == "ok"
    inject.assert_awaited()


def test_billing_sol_ticket_409_without_profile(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-sol", integration_type="whmcs")
    key = integration.plaintext_api_key
    owner = UserDAO.create(
        db_session,
        username="sol-bill",
        email="sol-bill@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-sol-1",
        external_username="sol-bill",
        external_email="sol-bill@example.com",
    )
    server = _server(db_session, profile=None, name="sol-bill-off")
    service = _bm_service(db_session, server, owner_user_id=owner.id, billing=True)
    resp = client.post(
        f"/api/billing/services/{service.id}/sol-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 409, resp.text


def test_billing_sol_ticket_and_status_flag(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-sol2", integration_type="whmcs")
    key = integration.plaintext_api_key
    owner = UserDAO.create(
        db_session,
        username="sol-bill2",
        email="sol-bill2@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-sol-2",
        external_username="sol-bill2",
        external_email="sol-bill2@example.com",
    )
    server = _server(db_session, name="sol-bill-on")
    service = _bm_service(db_session, server, owner_user_id=owner.id, billing=True)
    headers = {"Authorization": f"Bearer {key}"}

    status_resp = client.get(f"/api/billing/services/{service.id}/status", headers=headers)
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["sol_console_available"] is True

    ticket = client.post(f"/api/billing/services/{service.id}/sol-ticket", headers=headers)
    assert ticket.status_code == 200, ticket.text
    assert ticket.json()["launch_url"].startswith("https://rackflow.test/sol?t=")


def test_billing_sol_ticket_denied_when_bms_sol_false(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-sol3", integration_type="whmcs")
    key = integration.plaintext_api_key
    owner = UserDAO.create(
        db_session,
        username="sol-bill3",
        email="sol-bill3@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-sol-3",
        external_username="sol-bill3",
        external_email="sol-bill3@example.com",
    )
    server = _server(db_session, name="sol-bill-deny")
    service = _bm_service(
        db_session,
        server,
        owner_user_id=owner.id,
        billing=True,
        permission_overrides={PermissionKey.BMS_SOL: False},
    )
    resp = client.post(
        f"/api/billing/services/{service.id}/sol-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403, resp.text

    status_resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["sol_console_available"] is False
