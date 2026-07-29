"""Proxy runner config reflects service lifecycle (suspend/unsuspend/terminate)
and enforces location scoping, per the ipam-hardening + lifecycle-runner work.
"""
from app.dao.ipam_dao import IPAMDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.service_instance_dao import ServiceInstanceDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _proxy_service(db_session, *, location, status=ServiceStatus.ACTIVE, suffix="1"):
    server = ServerDAO.create(
        db_session,
        name=f"proxy-runner-srv-{suffix}",
        server_ip=f"10.60.0.{suffix}",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    return ServiceDAO.create_bare_metal(
        db_session,
        name=f"proxy-runner-svc-{suffix}",
        server_id=server.id,
        service_type=ServiceType.HTTP_PROXY,
        status=status,
        provisioning_source=ProvisioningSource.INTERNAL,
    )


def _runner_instance(db_session, location, suffix="1"):
    return ServiceInstanceDAO.create(
        db_session,
        location_id=location.id,
        service_type="proxy",
        name=f"proxy-runner-{suffix}",
        base_url=f"http://runner-{suffix}.local:8080",
        api_key=f"runner-secret-{suffix}",
    )


def _fetch_config(client, api_key):
    resp = client.get("/api/runner/proxy/config", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_suspended_service_excluded_from_runner_config(client, db_session):
    location = LocationDAO.create(db_session, name="loc-runner-suspend")
    _runner_instance(db_session, location)
    service = _proxy_service(db_session, location=location, status=ServiceStatus.ACTIVE)
    subnet = IPAMDAO.create_subnet(db_session, name="suspend-subnet", cidr="198.51.100.4/30", location_id=location.id)
    assignment = IPAMDAO.assign_ip(db_session, service_id=service.id, subnet_id=subnet.id, username="u", password="p")
    bind_ip = assignment.ip.ip_address

    data = _fetch_config(client, "runner-secret-1")
    assert any(row["bind_ip"] == bind_ip for row in data["assignments"])
    active_version = data["version"]

    service.status = ServiceStatus.SUSPENDED
    ServiceDAO.update(db_session, service)

    data2 = _fetch_config(client, "runner-secret-1")
    assert not any(row["bind_ip"] == bind_ip for row in data2["assignments"])
    # Version must change once the published assignment set changes so the
    # runner picks up the auth removal instead of treating it as "unchanged".
    assert data2["version"] != active_version


def test_unsuspended_service_reappears_in_runner_config(client, db_session):
    location = LocationDAO.create(db_session, name="loc-runner-unsuspend")
    _runner_instance(db_session, location, suffix="2")
    service = _proxy_service(db_session, location=location, status=ServiceStatus.SUSPENDED, suffix="2")
    subnet = IPAMDAO.create_subnet(db_session, name="unsuspend-subnet", cidr="198.51.100.8/30", location_id=location.id)
    assignment = IPAMDAO.assign_ip(db_session, service_id=service.id, subnet_id=subnet.id, username="u2", password="p2")
    bind_ip = assignment.ip.ip_address

    data = _fetch_config(client, "runner-secret-2")
    assert not any(row["bind_ip"] == bind_ip for row in data["assignments"])

    service.status = ServiceStatus.ACTIVE
    ServiceDAO.update(db_session, service)

    data2 = _fetch_config(client, "runner-secret-2")
    assert any(row["bind_ip"] == bind_ip and row["username"] == "u2" for row in data2["assignments"])


def test_terminate_releases_ipam_assignment_and_removes_from_config(client, db_session, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-runner-terminate")
    _runner_instance(db_session, location, suffix="3")
    service = _proxy_service(db_session, location=location, status=ServiceStatus.ACTIVE, suffix="3")
    subnet = IPAMDAO.create_subnet(db_session, name="terminate-subnet", cidr="198.51.100.12/30", location_id=location.id)
    assignment = IPAMDAO.assign_ip(db_session, service_id=service.id, subnet_id=subnet.id, username="u3", password="p3")
    bind_ip = assignment.ip.ip_address

    resp = client.put(
        f"/api/admin/services/{service.id}/status",
        headers=headers,
        json={"status": "terminated"},
    )
    assert resp.status_code == 200, resp.text

    remaining = IPAMDAO.get_assignment_by_service(db_session, service.id)
    assert remaining == []

    data = _fetch_config(client, "runner-secret-3")
    assert not any(row["bind_ip"] == bind_ip for row in data["assignments"])

    history = IPAMDAO.list_history(db_session, service_id=service.id)
    actions = [h.action for h in history]
    assert "released" in actions


def test_rotated_credentials_change_config_version(client, db_session):
    """Regression: the version hash used to be keyed off IPAddress.updated_at,
    which credential rotation never touches (only the assignment row's
    username/password change), so a rotated proxy password would silently
    keep serving stale creds to the runner until something else happened to
    touch the IP row. The version must change whenever published content
    (username/password) changes.
    """
    location = LocationDAO.create(db_session, name="loc-runner-rotate")
    _runner_instance(db_session, location, suffix="rotate")
    service = _proxy_service(db_session, location=location, suffix="rotate")
    subnet = IPAMDAO.create_subnet(db_session, name="rotate-subnet", cidr="198.51.100.28/30", location_id=location.id)
    assignment = IPAMDAO.assign_ip(db_session, service_id=service.id, subnet_id=subnet.id, username="u-before", password="p-before")

    before = _fetch_config(client, "runner-secret-rotate")
    before_row = next(row for row in before["assignments"] if row["bind_ip"] == assignment.ip.ip_address)
    assert before_row["username"] == "u-before"

    IPAMDAO.rotate_credentials(db_session, assignment_id=assignment.id, username="u-after", password="p-after", rotated_by="test")

    after = _fetch_config(client, "runner-secret-rotate")
    after_row = next(row for row in after["assignments"] if row["bind_ip"] == assignment.ip.ip_address)
    assert after_row["username"] == "u-after"
    assert after_row["password"] == "p-after"
    assert after["version"] != before["version"]


def test_location_scoping_hides_other_location_assignments(client, db_session):
    loc_a = LocationDAO.create(db_session, name="loc-runner-a")
    loc_b = LocationDAO.create(db_session, name="loc-runner-b")
    _runner_instance(db_session, loc_a, suffix="a")
    service_a = _proxy_service(db_session, location=loc_a, suffix="a")
    service_b = _proxy_service(db_session, location=loc_b, suffix="b")
    subnet_a = IPAMDAO.create_subnet(db_session, name="loc-a-subnet", cidr="198.51.100.16/30", location_id=loc_a.id)
    subnet_b = IPAMDAO.create_subnet(db_session, name="loc-b-subnet", cidr="198.51.100.20/30", location_id=loc_b.id)
    assign_a = IPAMDAO.assign_ip(db_session, service_id=service_a.id, subnet_id=subnet_a.id, username="ua")
    assign_b = IPAMDAO.assign_ip(db_session, service_id=service_b.id, subnet_id=subnet_b.id, username="ub")

    data = _fetch_config(client, "runner-secret-a")
    assert data["location_id"] == loc_a.id
    ips = {row["bind_ip"] for row in data["assignments"]}
    assert assign_a.ip.ip_address in ips
    assert assign_b.ip.ip_address not in ips


def test_config_version_stable_when_assignments_unchanged(client, db_session):
    location = LocationDAO.create(db_session, name="loc-runner-stable")
    _runner_instance(db_session, location, suffix="stable")
    service = _proxy_service(db_session, location=location, suffix="stable")
    subnet = IPAMDAO.create_subnet(db_session, name="stable-subnet", cidr="198.51.100.24/30", location_id=location.id)
    IPAMDAO.assign_ip(db_session, service_id=service.id, subnet_id=subnet.id, username="us")

    first = _fetch_config(client, "runner-secret-stable")
    second = _fetch_config(client, "runner-secret-stable")
    assert first["version"] == second["version"]
