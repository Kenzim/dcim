"""Windows password reset endpoints (admin, client portal, billing)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.client_permissions import PermissionKey
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceStatus
from app.services.sol.windows_password import WindowsPasswordResetError

_OK = {"status": "ok", "username": "Administrator", "generated": True, "password": "Gen3rated-pw"}


def _login(client, user, password):
    r = client.post("/api/users/login", json={"username": user.username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _service(db_session, name, *, owner_user_id=None, billing=False, overrides=None):
    location = LocationDAO.create(db_session, name=f"loc-{name}")
    server = ServerDAO.create(
        db_session,
        name=name,
        server_ip="10.16.251.200",
        plugin_name="ipmi",
        plugin_config={"hostname": "10.16.251.200", "username": "admin", "password": "secret"},
        location_id=location.id,
        sol_profile="ipmi_sol",
    )
    service = ServiceDAO.create_bare_metal(
        db_session,
        name=name,
        server_id=server.id,
        owner_user_id=owner_user_id,
        provisioning_source=ProvisioningSource.BILLING if billing else ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
    )
    service.config = {"template_id": "windows-server-2022"}
    if overrides is not None:
        service.permission_overrides = overrides
    ServiceDAO.update(db_session, service)
    return service


def _billing(db_session, suffix):
    integration = BillingIntegrationDAO.create(db_session, name=f"whmcs-wpr-{suffix}", integration_type="whmcs")
    owner = UserDAO.create(
        db_session,
        username=f"wpr-{suffix}",
        email=f"wpr-{suffix}@example.com",
        billing_integration_id=integration.id,
        external_user_id=f"ext-wpr-{suffix}",
        external_username=f"wpr-{suffix}",
        external_email=f"wpr-{suffix}@example.com",
    )
    return {"Authorization": f"Bearer {integration.plaintext_api_key}"}, owner


def test_admin_reset_generates(client, test_admin_user, db_session, monkeypatch):
    reset = AsyncMock(return_value=_OK)
    monkeypatch.setattr("app.api.sol.reset_windows_admin_password", reset)
    service = _service(db_session, "wpr-admin")
    resp = client.post(
        f"/api/admin/services/{service.id}/windows-password-reset",
        headers=_login(client, test_admin_user, "adminpassword123"),
        json={},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["password"] == "Gen3rated-pw"
    assert reset.await_args.args[3] is None
    assert reset.await_args.kwargs["source"] == "admin.service"


def test_client_portal_reset_and_flag(client, test_user, db_session, monkeypatch):
    reset = AsyncMock(return_value={"status": "ok", "username": "Administrator", "generated": False})
    monkeypatch.setattr("app.api.sol.reset_windows_admin_password", reset)
    monkeypatch.setattr(
        "app.services.sol.windows_password.get_template_service",
        lambda: SimpleNamespace(get_template=lambda _id: SimpleNamespace(os_type="windows")),
    )
    service = _service(db_session, "wpr-client", owner_user_id=test_user.id)
    headers = _login(client, test_user, "testpassword123")
    detail = client.get(f"/api/client/services/{service.id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["windows_password_reset_available"] is True
    resp = client.post(
        f"/api/client/services/{service.id}/windows-password-reset",
        headers=headers,
        json={"password": "Chosen-Pass1"},
    )
    assert resp.status_code == 200, resp.text
    assert "password" not in resp.json() or resp.json()["password"] is None
    assert reset.await_args.args[3] == "Chosen-Pass1"


def test_billing_client_audience_needs_sol_permission(client, db_session, monkeypatch):
    reset = AsyncMock(return_value=_OK)
    monkeypatch.setattr("app.api.sol.reset_windows_admin_password", reset)
    headers, owner = _billing(db_session, "deny")
    service = _service(
        db_session, "wpr-bill-deny", owner_user_id=owner.id, billing=True,
        overrides={PermissionKey.BMS_SOL: False},
    )
    url = f"/api/billing/services/{service.id}/windows-password-reset"
    assert client.post(url, headers=headers, json={}).status_code == 403
    reset.assert_not_awaited()
    # Admin audience (WHMCS admin module command) is not gated by client permissions.
    resp = client.post(f"{url}?audience=admin", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    assert reset.await_args.kwargs["source"] == "billing.service.admin"


def test_billing_maps_errors_and_reports_flag(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "app.api.sol.reset_windows_admin_password",
        AsyncMock(side_effect=WindowsPasswordResetError("No Administrator command prompt", 409)),
    )
    monkeypatch.setattr(
        "app.services.sol.windows_password.get_template_service",
        lambda: SimpleNamespace(get_template=lambda _id: SimpleNamespace(os_type="windows")),
    )
    headers, owner = _billing(db_session, "err")
    service = _service(db_session, "wpr-bill-err", owner_user_id=owner.id, billing=True)
    resp = client.post(
        f"/api/billing/services/{service.id}/windows-password-reset", headers=headers, json={}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "No Administrator command prompt"
    status_resp = client.get(f"/api/billing/services/{service.id}/status", headers=headers)
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["windows_password_reset_available"] is True
