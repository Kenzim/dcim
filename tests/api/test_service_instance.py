"""Tests for the service-instance API's base_url validation."""
import pytest

from app.models.location import Location


@pytest.fixture
def test_location(db_session):
    location = Location(name="SI Test Location", description="test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)
    return location


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_create_service_instance_accepts_http_base_url(client, test_admin_user, mock_redis, test_location):
    headers = _admin_headers(client, test_admin_user)
    response = client.post(
        "/api/service-instances/",
        headers=headers,
        json={
            "location_id": test_location.id,
            "service_type": "dhcp",
            "name": "dhcp-runner",
            "base_url": "http://10.50.0.5:9080",
            "api_key": "secret",
        },
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "gopher://127.0.0.1:9080/",
        "ftp://10.50.0.5/",
        "not-a-url",
    ],
)
def test_create_service_instance_rejects_non_http_base_url(
    client, test_admin_user, mock_redis, test_location, bad_url
):
    headers = _admin_headers(client, test_admin_user)
    response = client.post(
        "/api/service-instances/",
        headers=headers,
        json={
            "location_id": test_location.id,
            "service_type": "tftp",
            "name": "tftp-runner",
            "base_url": bad_url,
            "api_key": "secret",
        },
    )
    assert response.status_code == 422


def test_update_service_instance_rejects_non_http_base_url(client, test_admin_user, mock_redis, test_location):
    headers = _admin_headers(client, test_admin_user)
    create_response = client.post(
        "/api/service-instances/",
        headers=headers,
        json={
            "location_id": test_location.id,
            "service_type": "dhcp",
            "name": "dhcp-runner-update",
            "base_url": "http://10.50.0.6:9080",
            "api_key": "secret",
        },
    )
    assert create_response.status_code == 201
    instance_id = create_response.json()["id"]

    update_response = client.put(
        f"/api/service-instances/{instance_id}",
        headers=headers,
        json={"base_url": "file:///etc/shadow"},
    )
    assert update_response.status_code == 422
