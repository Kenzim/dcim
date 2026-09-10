"""IPMI HTML5 KVM API: profiles, redeem, tickets, popups, status flag."""
from unittest.mock import AsyncMock

from app.core.client_permissions import PermissionKey
from app.core.config import settings
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceStatus
from app.services.ipmi_kvm.base import BmcKvmAuth
from app.services.ipmi_kvm.hub_lock import cache_kvm_asset
from app.services.ipmi_kvm_ticket_service import mint_launch_ticket, mint_viewer_session, mint_ws_session


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


def _server(db_session, *, profile="asrockrack", name="kvm-srv"):
    location = LocationDAO.create(db_session, name=f"loc-{name}")
    return ServerDAO.create(
        db_session,
        name=name,
        server_ip="10.16.251.138",
        plugin_name="ipmi",
        plugin_config={"hostname": "10.16.251.138", "username": "admin", "password": "secret"},
        location_id=location.id,
        ipmi_kvm_profile=profile,
        ipmi_web_management_url="https://10.16.251.138",
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
        name="bm-kvm",
        server_id=server.id,
        owner_user_id=owner_user_id,
        **kwargs,
    )
    if permission_overrides is not None:
        service.permission_overrides = permission_overrides
        ServiceDAO.update(db_session, service)
    return service


def _fake_auth():
    return BmcKvmAuth(
        https_base="https://10.16.251.138",
        origin="https://10.16.251.138",
        hostname="10.16.251.138",
        cookie="qsess",
        csrf="csrf",
        kvm_token="kvm-tok",
        client_ip="10.0.0.8",
        username="admin",
    )


def test_admin_lists_kvm_profiles(client, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    resp = client.get("/api/ipmi-kvm/profiles", headers=headers)
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert "asrockrack" in ids
    assert "gigabyte" in ids
    assert "supermicro" in ids


def test_create_server_rejects_unknown_kvm_profile(client, test_admin_user, db_session):
    headers = _login_admin(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-bad-kvm")
    resp = client.post(
        "/api/servers/",
        headers=headers,
        json={
            "name": "bad-kvm-profile",
            "server_ip": "10.9.9.9",
            "location_id": location.id,
            "plugin_name": "ipmi",
            "plugin_config": {"hostname": "10.9.9.9", "username": "admin", "password": "x"},
            "ipmi_kvm_profile": "idrac",
        },
    )
    assert resp.status_code == 400, resp.text


def test_redeem_invalid_token_is_400(client):
    resp = client.post("/api/kvm/redeem", json={"token": "nope"})
    assert resp.status_code == 400, resp.text


def test_redeem_without_profile_is_409(client, db_session):
    server = _server(db_session, profile=None, name="kvm-off")
    token = mint_launch_ticket(server.id)
    resp = client.post("/api/kvm/redeem", json={"token": token})
    assert resp.status_code == 409, resp.text


def test_redeem_mints_ws_session_without_bmc_secrets(client, db_session, monkeypatch):
    server = _server(db_session)
    login = AsyncMock(return_value=_fake_auth())
    monkeypatch.setattr(
        "app.services.ipmi_kvm.asrockrack.AsrockRackKvmProfile.login",
        login,
    )
    token = mint_launch_ticket(server.id)
    resp = client.post("/api/kvm/redeem", json={"token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ws_path"] == "/api/kvm/ws"
    assert body["profile"] == "asrockrack"
    assert "decode_worker_path" in body
    assert "kvm_token" not in body
    assert "cookie" not in body
    assert client.post("/api/kvm/redeem", json={"token": token}).status_code == 400
    login.assert_not_called()


def test_asset_rejects_path_traversal(client, db_session):
    minted = mint_ws_session(
        1,
        "asrockrack",
        https_base="https://bmc.example",
        cookie="c",
        csrf="x",
        kvm_token="t",
        client_ip="1.1.1.1",
        username="admin",
        hostname="bmc.example",
    )
    resp = client.get(
        f"/api/kvm/assets/libs/kvm/../../secret.js?token={minted['ws_token']}"
    )
    assert resp.status_code in (400, 404)


def test_asset_proxies_allowlisted_path(client, db_session, monkeypatch):
    server = _server(db_session)
    minted = mint_ws_session(
        server.id,
        "asrockrack",
        https_base="https://10.16.251.138",
        cookie="c",
        csrf="x",
        kvm_token="t",
        client_ip="1.1.1.1",
        username="admin",
        hostname="10.16.251.138",
    )
    monkeypatch.setattr(
        "app.services.ipmi_kvm.asrockrack.AsrockRackKvmProfile.fetch_asset",
        AsyncMock(return_value=(b"/* worker */", "application/javascript")),
    )
    resp = client.get(
        f"/api/kvm/assets/libs/kvm/ast/decode_worker.js?token={minted['ws_token']}"
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == b"/* worker */"


def test_asset_serves_cached_decode_worker_for_viewer_ticket(client, db_session):
    server = _server(db_session)
    minted = mint_viewer_session(server.id, "asrockrack")
    cache_kvm_asset(
        server.id,
        "libs/kvm/ast/decode_worker.js",
        b"/* cached worker */",
        "application/javascript",
    )
    resp = client.get(
        f"/api/kvm/assets/libs/kvm/ast/decode_worker.js?token={minted['ws_token']}"
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == b"/* cached worker */"


def test_admin_server_kvm_popup_without_profile_errors(client, test_admin_user, db_session):
    headers = _login_admin(client, test_admin_user)
    server = _server(db_session, profile=None, name="kvm-popup-off")
    resp = client.get(f"/api/servers/{server.id}/kvm-popup", headers=headers, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/kvm?e=")


def test_admin_server_kvm_popup_with_profile(client, test_admin_user, db_session):
    headers = _login_admin(client, test_admin_user)
    server = _server(db_session, name="kvm-popup-on")
    resp = client.get(f"/api/servers/{server.id}/kvm-popup", headers=headers, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/kvm?t=")


def test_client_kvm_popup_denied_without_permission(client, db_session, test_user):
    headers = _login_user(client, test_user)
    server = _server(db_session, name="kvm-client-deny")
    service = _bm_service(
        db_session,
        server,
        owner_user_id=test_user.id,
        permission_overrides={PermissionKey.BMS_KVM: False},
    )
    resp = client.get(
        f"/api/client/services/{service.id}/kvm-popup", headers=headers, follow_redirects=False
    )
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/kvm?e=")


def test_client_kvm_popup_when_permitted(client, db_session, test_user):
    headers = _login_user(client, test_user)
    server = _server(db_session, name="kvm-client-ok")
    service = _bm_service(db_session, server, owner_user_id=test_user.id)
    resp = client.get(
        f"/api/client/services/{service.id}/kvm-popup", headers=headers, follow_redirects=False
    )
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/kvm?t=")


def test_client_kvm_popup_when_ipmi_proxy_denied(client, db_session, test_user):
    headers = _login_user(client, test_user)
    server = _server(db_session, name="kvm-client-split")
    service = _bm_service(
        db_session,
        server,
        owner_user_id=test_user.id,
        permission_overrides={PermissionKey.BMS_IPMI: False},
    )
    resp = client.get(
        f"/api/client/services/{service.id}/kvm-popup", headers=headers, follow_redirects=False
    )
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/kvm?t=")


def test_billing_kvm_ticket_409_without_profile(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-kvm", integration_type="whmcs")
    key = integration.plaintext_api_key
    owner = UserDAO.create(
        db_session,
        username="kvm-bill",
        email="kvm-bill@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-kvm-1",
        external_username="kvm-bill",
        external_email="kvm-bill@example.com",
    )
    server = _server(db_session, profile=None, name="kvm-bill-off")
    service = _bm_service(db_session, server, owner_user_id=owner.id, billing=True)
    resp = client.post(
        f"/api/billing/services/{service.id}/kvm-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 409, resp.text


def test_billing_kvm_ticket_and_status_flag(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-kvm2", integration_type="whmcs")
    key = integration.plaintext_api_key
    owner = UserDAO.create(
        db_session,
        username="kvm-bill2",
        email="kvm-bill2@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-kvm-2",
        external_username="kvm-bill2",
        external_email="kvm-bill2@example.com",
    )
    server = _server(db_session, name="kvm-bill-on")
    service = _bm_service(db_session, server, owner_user_id=owner.id, billing=True)
    headers = {"Authorization": f"Bearer {key}"}

    status_resp = client.get(f"/api/billing/services/{service.id}/status", headers=headers)
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["kvm_console_available"] is True

    ticket = client.post(f"/api/billing/services/{service.id}/kvm-ticket", headers=headers)
    assert ticket.status_code == 200, ticket.text
    assert ticket.json()["launch_url"].startswith("https://rackflow.test/kvm?t=")


def test_billing_kvm_ticket_denied_when_bms_kvm_false(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-kvm3", integration_type="whmcs")
    key = integration.plaintext_api_key
    owner = UserDAO.create(
        db_session,
        username="kvm-bill3",
        email="kvm-bill3@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-kvm-3",
        external_username="kvm-bill3",
        external_email="kvm-bill3@example.com",
    )
    server = _server(db_session, name="kvm-bill-deny")
    service = _bm_service(
        db_session,
        server,
        owner_user_id=owner.id,
        billing=True,
        permission_overrides={PermissionKey.BMS_KVM: False},
    )
    resp = client.post(
        f"/api/billing/services/{service.id}/kvm-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403, resp.text

    status_resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["kvm_console_available"] is False


def test_billing_kvm_ticket_allowed_when_ipmi_proxy_denied(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-kvm4", integration_type="whmcs")
    key = integration.plaintext_api_key
    owner = UserDAO.create(
        db_session,
        username="kvm-bill4",
        email="kvm-bill4@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-kvm-4",
        external_username="kvm-bill4",
        external_email="kvm-bill4@example.com",
    )
    server = _server(db_session, name="kvm-bill-split")
    service = _bm_service(
        db_session,
        server,
        owner_user_id=owner.id,
        billing=True,
        permission_overrides={PermissionKey.BMS_IPMI: False},
    )
    headers = {"Authorization": f"Bearer {key}"}
    status_resp = client.get(f"/api/billing/services/{service.id}/status", headers=headers)
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["kvm_console_available"] is True
    ticket = client.post(f"/api/billing/services/{service.id}/kvm-ticket", headers=headers)
    assert ticket.status_code == 200, ticket.text
    deny_proxy = client.post(f"/api/billing/services/{service.id}/ipmi-ticket", headers=headers)
    assert deny_proxy.status_code == 403, deny_proxy.text
