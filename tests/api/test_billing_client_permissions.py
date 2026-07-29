"""
Billing API endpoints respect resolved client permissions: denied actions
return 403, and /status reports client_permissions + hides IPMI viewer
fields when bms.ipmi is denied.
"""
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.user_dao import UserDAO
from app.dao.location_dao import LocationDAO
from app.dao.permission_set_dao import PermissionSetDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.core.client_permissions import PermissionKey
from app.models.service import ProvisioningSource, ServiceStatus


def _integration(db_session, name="whmcs-perms"):
    integration = BillingIntegrationDAO.create(db_session, name=name, integration_type="whmcs")
    return integration, integration.plaintext_api_key


def _bare_metal_service_for_billing(db_session, integration, *, permission_overrides=None):
    billing_user = UserDAO.create(
        db_session,
        username="permsuser",
        email="perms@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-perms-1",
        external_username="permsuser",
        external_email="perms@example.com",
    )
    location = LocationDAO.create(db_session, name="loc-perms-1")
    server = ServerDAO.create(
        db_session,
        name="srv-perms-1",
        server_ip="10.20.30.40",
        plugin_name="ipmi",
        plugin_config={},
        location_id=location.id,
        ipmi_proxy_enabled=True,
        ipmi_web_management_url="https://bmc.example.com",
        ipmi_viewer_username="viewer",
        ipmi_viewer_password="secret",
    )
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="svc-perms-1",
        server_id=server.id,
        owner_user_id=billing_user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
    )
    if permission_overrides is not None:
        service.permission_overrides = permission_overrides
        ServiceDAO.update(db_session, service)
    return service


def test_billing_power_not_gated_by_bms_power_permission(client, db_session):
    """Billing API key is operator authority; BMS_POWER only gates client UI/routes."""
    integration, key = _integration(db_session)
    service = _bare_metal_service_for_billing(
        db_session, integration, permission_overrides={PermissionKey.BMS_POWER: False}
    )

    resp = client.post(
        f"/api/billing/services/{service.id}/power",
        headers={"Authorization": f"Bearer {key}"},
        json={"action": "off"},
    )
    # May fail at the plugin/BMC layer, but must not be blocked by client permissions.
    assert resp.status_code != 403, resp.text


def test_power_allowed_by_default(client, db_session):
    integration, key = _integration(db_session)
    service = _bare_metal_service_for_billing(db_session, integration)

    resp = client.post(
        f"/api/billing/services/{service.id}/power",
        headers={"Authorization": f"Bearer {key}"},
        json={"action": "off"},
    )
    # Reaches the plugin layer (no real BMC), so it may fail downstream, but
    # it must not be blocked by permissions (403).
    assert resp.status_code != 403, resp.text


def test_ipmi_ticket_denied_when_bms_ipmi_false(client, db_session):
    integration, key = _integration(db_session)
    service = _bare_metal_service_for_billing(
        db_session, integration, permission_overrides={PermissionKey.BMS_IPMI: False}
    )

    resp = client.post(
        f"/api/billing/services/{service.id}/ipmi-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403, resp.text


def test_portal_sso_denied_when_service_portal_false(client, db_session):
    integration, key = _integration(db_session)
    service = _bare_metal_service_for_billing(
        db_session, integration, permission_overrides={PermissionKey.SERVICE_PORTAL: False}
    )

    resp = client.post(
        f"/api/billing/services/{service.id}/portal-sso",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403, resp.text


def test_run_script_denied_by_default(client, db_session):
    """bms.run_script defaults to False, matching pre-existing behavior
    where scripts were gated purely by Script.user_executable."""
    integration, key = _integration(db_session)
    service = _bare_metal_service_for_billing(db_session, integration)

    resp = client.post(
        f"/api/billing/services/{service.id}/actions/run-script",
        headers={"Authorization": f"Bearer {key}"},
        json={"script_id": 1},
    )
    assert resp.status_code == 403, resp.text


def test_reinstall_denied_by_default(client, db_session):
    integration, key = _integration(db_session)
    service = _bare_metal_service_for_billing(db_session, integration)

    resp = client.post(
        f"/api/billing/services/{service.id}/actions/reinstall-os",
        headers={"Authorization": f"Bearer {key}"},
        json={"template_id": "debian-12"},
    )
    assert resp.status_code == 403, resp.text


def test_reinstall_allowed_with_preset(client, db_session):
    integration, key = _integration(db_session)
    preset = PermissionSetDAO.create(
        db_session, name="reinstall-ok", permissions={PermissionKey.BMS_REINSTALL: True}
    )
    service = _bare_metal_service_for_billing(db_session, integration)
    service.permission_set_id = preset.id
    ServiceDAO.update(db_session, service)

    resp = client.post(
        f"/api/billing/services/{service.id}/actions/reinstall-os",
        headers={"Authorization": f"Bearer {key}"},
        json={"template_id": "unknown-template"},
    )
    # Permission check passes; fails downstream (404, unknown template) —
    # the key assertion is that it's not blocked by permissions (403).
    assert resp.status_code != 403, resp.text


def test_status_reports_client_permissions_and_hides_ipmi_when_denied(client, db_session):
    integration, key = _integration(db_session)
    service = _bare_metal_service_for_billing(
        db_session, integration, permission_overrides={PermissionKey.BMS_IPMI: False}
    )

    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_permissions"][PermissionKey.BMS_IPMI] is False
    assert body["ipmi_proxy_available"] is False
    assert body["ipmi_viewer_username"] is None
    assert body["ipmi_viewer_password"] is None


def test_status_exposes_ipmi_viewer_fields_when_granted(client, db_session):
    integration, key = _integration(db_session)
    service = _bare_metal_service_for_billing(db_session, integration)

    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_permissions"][PermissionKey.BMS_IPMI] is True
    assert body["ipmi_proxy_available"] is True
    assert body["ipmi_viewer_username"] == "viewer"
    assert body["power_available"] is True
