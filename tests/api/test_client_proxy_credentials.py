"""Client portal view/rotate proxy credential endpoints (services_client.py):
ownership checks, service_type gating, and default permission behavior
(view on by default, rotate off by default until granted via preset/override).
"""
from app.core.client_permissions import PermissionKey
from app.dao.ipam_dao import IPAMDAO
from app.dao.location_dao import LocationDAO
from app.dao.permission_set_dao import PermissionSetDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.models.user import User


def _login(client, username, password="secret123"):
    resp = client.post("/api/users/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _client_user(db_session, username):
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _proxy_service(db_session, *, owner_user_id, suffix="1", permission_overrides=None, permission_set_id=None):
    location = LocationDAO.create(db_session, name=f"loc-client-proxy-{suffix}")
    subnet = IPAMDAO.create_subnet(
        db_session, name=f"client-proxy-subnet-{suffix}", cidr=f"203.0.116.{int(suffix) * 8}/29", location_id=location.id
    )
    service = ServiceDAO.create_bare_metal(
        db_session,
        name=f"svc-client-proxy-{suffix}",
        server_id=None,
        owner_user_id=owner_user_id,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    IPAMDAO.assign_ip(db_session, service_id=service.id, subnet_id=subnet.id, username="cu", password="cp")
    if permission_overrides is not None:
        service.permission_overrides = permission_overrides
    if permission_set_id is not None:
        service.permission_set_id = permission_set_id
    if permission_overrides is not None or permission_set_id is not None:
        ServiceDAO.update(db_session, service)
    return service


def test_client_can_view_proxy_credentials_by_default(client, db_session):
    user = _client_user(db_session, "proxyclient1")
    service = _proxy_service(db_session, owner_user_id=user.id, suffix="1")
    token = _login(client, "proxyclient1")

    resp = client.get(f"/api/services/{service.id}/proxy/credentials", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assignments = resp.json()["assignments"]
    assert len(assignments) == 1
    assert assignments[0]["username"] == "cu"
    assert assignments[0]["http_url"].startswith("http://cu:cp@")
    assert assignments[0]["port"] == 8080
    assert assignments[0]["endpoint"] == f"{assignments[0]['ip_address']}:8080:cu:cp"


def test_client_rotate_denied_by_default(client, db_session):
    user = _client_user(db_session, "proxyclient2")
    service = _proxy_service(db_session, owner_user_id=user.id, suffix="2")
    token = _login(client, "proxyclient2")

    resp = client.post(f"/api/services/{service.id}/proxy/rotate", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, resp.text


def test_client_rotate_allowed_with_preset(client, db_session):
    preset = PermissionSetDAO.create(
        db_session, name="proxy-rotate-ok", permissions={PermissionKey.PROXY_ROTATE_CREDENTIALS: True}
    )
    user = _client_user(db_session, "proxyclient3")
    service = _proxy_service(db_session, owner_user_id=user.id, suffix="3", permission_set_id=preset.id)
    token = _login(client, "proxyclient3")

    before = IPAMDAO.get_assignment_by_service(db_session, service.id)[0]
    before_username = before.username

    resp = client.post(f"/api/services/{service.id}/proxy/rotate", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    rotated = resp.json()["assignments"]
    assert len(rotated) == 1
    assert rotated[0]["username"] != before_username


def test_client_cannot_view_credentials_for_unowned_service(client, db_session):
    owner = _client_user(db_session, "proxyowner4")
    other = _client_user(db_session, "proxyother4")
    service = _proxy_service(db_session, owner_user_id=owner.id, suffix="4")
    token = _login(client, "proxyother4")

    resp = client.get(f"/api/services/{service.id}/proxy/credentials", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404, resp.text


def test_client_view_denied_when_explicitly_overridden_off(client, db_session):
    user = _client_user(db_session, "proxyclient5")
    service = _proxy_service(
        db_session, owner_user_id=user.id, suffix="5", permission_overrides={PermissionKey.PROXY_VIEW_CREDENTIALS: False}
    )
    token = _login(client, "proxyclient5")

    resp = client.get(f"/api/services/{service.id}/proxy/credentials", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, resp.text


def test_client_proxy_endpoints_reject_non_proxy_service(client, db_session):
    user = _client_user(db_session, "proxyclient6")
    service = ServiceDAO.create_vm(
        db_session,
        name="svc-client-not-proxy",
        owner_user_id=user.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
    )
    token = _login(client, "proxyclient6")

    resp = client.get(f"/api/services/{service.id}/proxy/credentials", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400, resp.text


def test_client_portal_mirror_endpoints_match_services_client(client, db_session):
    """The /api/client mirror in app/api/client.py must behave the same as
    /api/services for proxy credentials (kept in sync intentionally)."""
    user = _client_user(db_session, "proxyclient7")
    service = _proxy_service(db_session, owner_user_id=user.id, suffix="7")
    token = _login(client, "proxyclient7")

    resp = client.get(f"/api/client/services/{service.id}/proxy/credentials", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["assignments"][0]["username"] == "cu"
