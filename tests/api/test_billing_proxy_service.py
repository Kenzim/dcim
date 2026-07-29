"""Billing (WHMCS) surface for http_proxy: server-less create with catalog
auto-assign, credential rotate, and terminate releasing IPAM assignments.
"""
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.user_dao import UserDAO
from app.dao.ipam_dao import IPAMDAO
from app.dao.location_dao import LocationDAO
from app.dao.permission_set_dao import PermissionSetDAO
from app.dao.service_dao import ServiceDAO
from app.core.client_permissions import PermissionKey
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType


def _integration(db_session, name="whmcs-proxy"):
    integration = BillingIntegrationDAO.create(db_session, name=name, integration_type="whmcs")
    return integration, integration.plaintext_api_key


def _ext_user(db_session, integration, suffix="1"):
    return UserDAO.create(
        db_session,
        username=f"proxyuser{suffix}",
        email=f"proxy{suffix}@example.com",
        billing_integration_id=integration.id,
        external_user_id=f"ext-proxy-{suffix}",
        external_username=f"proxyuser{suffix}",
        external_email=f"proxy{suffix}@example.com",
    )


def _proxy_service_for_billing(db_session, integration, *, suffix="1", permission_overrides=None):
    billing_user = _ext_user(db_session, integration, suffix)
    location = LocationDAO.create(db_session, name=f"loc-billing-proxy-{suffix}")
    subnet = IPAMDAO.create_subnet(
        db_session, name=f"billing-proxy-subnet-{suffix}", cidr=f"203.0.114.{int(suffix) * 8}/29", location_id=location.id
    )
    service = ServiceDAO.create_bare_metal(
        db_session,
        name=f"svc-billing-proxy-{suffix}",
        server_id=None,
        owner_user_id=billing_user.id,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.BILLING,
    )
    IPAMDAO.assign_ip(db_session, service_id=service.id, subnet_id=subnet.id, username="wu", password="wp")
    if permission_overrides is not None:
        service.permission_overrides = permission_overrides
        ServiceDAO.update(db_session, service)
    return service


def test_billing_create_http_proxy_auto_assigns_multiple_ips_from_catalog(client, db_session, test_admin_user):
    integration, key = _integration(db_session)
    location = LocationDAO.create(db_session, name="loc-billing-create")
    subnet = IPAMDAO.create_subnet(db_session, name="billing-create-subnet", cidr="203.0.115.0/28", location_id=location.id)

    family = client.post(
        "/api/product-catalog/families",
        headers={"Authorization": f"Bearer {_admin_token(client)}"},
        json={
            "name": "Billing Proxy Family",
            "code": "billing-proxy-family",
            "service_type": "http_proxy",
            "defaults": {"ip_count": 3, "subnet_id": subnet.id},
        },
    )
    assert family.status_code == 201, family.text
    product = client.post(
        "/api/product-catalog/products",
        headers={"Authorization": f"Bearer {_admin_token(client)}"},
        json={"family_id": family.json()["id"], "name": "Billing Proxy Product", "code": "billing-proxy-product"},
    )
    assert product.status_code == 201, product.text

    resp = client.post(
        "/api/billing/bare-metal/services",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "name": "whmcs-proxy-svc-1",
            "external_user_id": "whmcs-client-1",
            "external_username": "whmcsclient1",
            "external_email": "whmcsclient1@example.com",
            "service_type": "http_proxy",
            "product_code": "billing-proxy-product",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"

    service = ServiceDAO.get_by_name(db_session, "whmcs-proxy-svc-1")
    assignments = IPAMDAO.get_assignment_by_service(db_session, service.id)
    assert len(assignments) == 3


def _admin_token(client):
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def test_billing_rotate_proxy_credentials_changes_username_and_password(client, db_session):
    integration, key = _integration(db_session, name="whmcs-proxy-rotate")
    service = _proxy_service_for_billing(
        db_session, integration, suffix="2", permission_overrides={PermissionKey.PROXY_ROTATE_CREDENTIALS: True}
    )
    before = IPAMDAO.get_assignment_by_service(db_session, service.id)[0]
    before_username, before_password, before_ip = before.username, before.password, before.ip.ip_address

    resp = client.post(
        f"/api/billing/services/{service.id}/proxy/rotate",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    rotated = resp.json()["proxy_assignments"]
    assert len(rotated) == 1
    assert rotated[0]["username"] != before_username
    assert rotated[0]["password"] != before_password
    assert rotated[0]["ip_address"] == before_ip


def test_billing_rotate_proxy_credentials_denied_by_default(client, db_session):
    """proxy.rotate_credentials defaults to False; only view is on by default."""
    integration, key = _integration(db_session, name="whmcs-proxy-rotate-default")
    service = _proxy_service_for_billing(db_session, integration, suffix="6")
    resp = client.post(
        f"/api/billing/services/{service.id}/proxy/rotate",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403, resp.text


def test_billing_rotate_proxy_credentials_rejects_non_proxy_service(client, db_session):
    integration, key = _integration(db_session, name="whmcs-proxy-notproxy")
    billing_user = _ext_user(db_session, integration, suffix="np")
    service = ServiceDAO.create_vm(
        db_session,
        name="svc-not-a-proxy",
        owner_user_id=billing_user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
    )
    resp = client.post(
        f"/api/billing/services/{service.id}/proxy/rotate",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400, resp.text


def test_billing_terminate_releases_proxy_ipam_assignments(client, db_session):
    integration, key = _integration(db_session, name="whmcs-proxy-terminate")
    service = _proxy_service_for_billing(db_session, integration, suffix="3")

    resp = client.delete(
        f"/api/billing/services/{service.id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 204, resp.text

    remaining = IPAMDAO.get_assignment_by_service(db_session, service.id)
    assert remaining == []


def test_billing_status_hides_proxy_credentials_when_permission_denied(client, db_session):
    integration, key = _integration(db_session, name="whmcs-proxy-status-denied")
    service = _proxy_service_for_billing(
        db_session, integration, suffix="4", permission_overrides={PermissionKey.PROXY_VIEW_CREDENTIALS: False}
    )
    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proxy_credentials_available"] is False
    assert body["proxy_assignments"] is None


def test_billing_status_exposes_proxy_credentials_when_granted(client, db_session):
    integration, key = _integration(db_session, name="whmcs-proxy-status-granted")
    service = _proxy_service_for_billing(db_session, integration, suffix="5")
    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proxy_credentials_available"] is True
    assert body["proxy_assignments"] and len(body["proxy_assignments"]) == 1
    assert body["proxy_assignments"][0]["username"] == "wu"
    # Proxy has no linked rack Server; power controls must not be offered.
    assert body["power_available"] is False
