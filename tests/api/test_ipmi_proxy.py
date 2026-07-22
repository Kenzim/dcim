"""
Tests for the end-user IPMI reverse-proxy endpoints:
- billing mint (owner + non-owner)
- runner config + ticket redeem (auth)
- disabled-proxy 409
"""
import pytest

from app.core.billing_auth import hash_api_key
from app.core.config import settings
from app.models.billing_integration import BillingIntegration
from app.models.external_user import ExternalUser
from app.models.location import Location
from app.models.server import Server
from app.models.service import Service, ServiceStatus, ServiceType, ProvisioningSource
from app.models.service_bare_metal import ServiceBareMetal


PUBLIC_BASE = "ipmi.test"
RUNNER_KEY = "runner-key-1"


@pytest.fixture(autouse=True)
def _ipmi_settings(monkeypatch):
    monkeypatch.setattr(settings, "ipmi_proxy_public_base", PUBLIC_BASE)
    monkeypatch.setattr(settings, "ipmi_proxy_runner_api_key", RUNNER_KEY)
    monkeypatch.setattr(settings, "ipmi_proxy_scheme", "https")
    monkeypatch.setattr(settings, "ipmi_proxy_port", None)


def _make_integration(db_session, api_key="key-1", name="Integration A"):
    integration = BillingIntegration(
        name=name,
        integration_type="whmcs",
        api_key=hash_api_key(api_key),
        enabled=True,
    )
    db_session.add(integration)
    db_session.commit()
    db_session.refresh(integration)
    return integration


def _make_server(db_session, location, *, enabled_proxy=True):
    server = Server(
        name="bm-srv",
        server_ip="10.50.0.10",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={},
        ipmi_proxy_enabled=enabled_proxy,
        ipmi_web_management_url="https://10.99.0.5" if enabled_proxy else None,
        ipmi_viewer_username="viewer" if enabled_proxy else None,
        ipmi_viewer_password="secret" if enabled_proxy else None,
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def _make_service(db_session, integration, server):
    ext_user = ExternalUser(
        integration_id=integration.id,
        external_user_id="client-1",
        external_username="client1",
        external_email="client1@example.com",
    )
    db_session.add(ext_user)
    db_session.commit()
    db_session.refresh(ext_user)

    service = Service(
        name="bm-service",
        external_service_id="svc-1",
        external_user_id=ext_user.id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.ACTIVE,
        config={},
        provisioning_source=ProvisioningSource.BILLING,
        bare_metal=ServiceBareMetal(server_id=server.id),
    )
    db_session.add(service)
    db_session.commit()
    db_session.refresh(service)
    return service


@pytest.fixture
def location(db_session):
    loc = Location(name="dc1")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    return loc


def test_billing_mint_owner_ok(client, db_session, location):
    integration = _make_integration(db_session)
    server = _make_server(db_session, location)
    service = _make_service(db_session, integration, server)

    resp = client.post(
        f"/api/billing/services/{service.id}/ipmi-ticket",
        headers={"Authorization": "Bearer key-1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["launch_url"].startswith(f"https://{server.uuid}.{PUBLIC_BASE}/__ipmi/auth?t=")
    assert data["proxy_url"] == f"https://{server.uuid}.{PUBLIC_BASE}"
    assert data["viewer_username"] == "viewer"
    assert data["viewer_password"] == "secret"


def test_billing_status_includes_viewer_credentials(client, db_session, location):
    integration = _make_integration(db_session)
    server = _make_server(db_session, location)
    service = _make_service(db_session, integration, server)

    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": "Bearer key-1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ipmi_proxy_available"] is True
    assert data["ipmi_viewer_username"] == "viewer"
    assert data["ipmi_viewer_password"] == "secret"


def test_billing_status_hides_viewer_credentials_when_proxy_disabled(
    client, db_session, location
):
    integration = _make_integration(db_session)
    server = _make_server(db_session, location, enabled_proxy=False)
    service = _make_service(db_session, integration, server)

    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": "Bearer key-1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ipmi_proxy_available"] is False
    assert data["ipmi_viewer_username"] is None
    assert data["ipmi_viewer_password"] is None


def test_billing_mint_non_owner_404(client, db_session, location):
    integration_a = _make_integration(db_session, api_key="key-1", name="A")
    integration_b = _make_integration(db_session, api_key="key-2", name="B")
    server = _make_server(db_session, location)
    service = _make_service(db_session, integration_a, server)

    # Another integration's key must not see this service.
    resp = client.post(
        f"/api/billing/services/{service.id}/ipmi-ticket",
        headers={"Authorization": "Bearer key-2"},
    )
    assert resp.status_code == 404, resp.text


def test_billing_mint_disabled_proxy_409(client, db_session, location):
    integration = _make_integration(db_session)
    server = _make_server(db_session, location, enabled_proxy=False)
    service = _make_service(db_session, integration, server)

    resp = client.post(
        f"/api/billing/services/{service.id}/ipmi-ticket",
        headers={"Authorization": "Bearer key-1"},
    )
    assert resp.status_code == 409, resp.text


def test_runner_config_requires_key(client, db_session, location):
    _make_server(db_session, location)

    # No key -> 401
    resp = client.get("/api/runner/ipmi/config")
    assert resp.status_code == 401

    # Wrong key -> 401
    resp = client.get(
        "/api/runner/ipmi/config", headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401


def test_runner_config_lists_enabled_servers(client, db_session, location):
    server = _make_server(db_session, location)

    resp = client.get(
        "/api/runner/ipmi/config",
        headers={"Authorization": f"Bearer {RUNNER_KEY}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    uuids = {row["uuid"]: row["upstream_url"] for row in data["servers"]}
    assert uuids.get(server.uuid) == "https://10.99.0.5"


def test_redeem_flow_end_to_end(client, db_session, location):
    integration = _make_integration(db_session)
    server = _make_server(db_session, location)
    service = _make_service(db_session, integration, server)

    # Mint via billing.
    mint = client.post(
        f"/api/billing/services/{service.id}/ipmi-ticket",
        headers={"Authorization": "Bearer key-1"},
    )
    assert mint.status_code == 200, mint.text
    launch_url = mint.json()["launch_url"]
    token = launch_url.split("t=", 1)[1]

    # Redeem as the edge runner.
    redeem = client.post(
        "/api/runner/ipmi/redeem",
        headers={"Authorization": f"Bearer {RUNNER_KEY}"},
        json={"token": token, "host_uuid": server.uuid},
    )
    assert redeem.status_code == 200, redeem.text
    body = redeem.json()
    assert body["uuid"] == server.uuid
    assert body["upstream_url"] == "https://10.99.0.5"
    assert body["session_ttl"] >= 1

    # Single-use: second redeem fails.
    redeem2 = client.post(
        "/api/runner/ipmi/redeem",
        headers={"Authorization": f"Bearer {RUNNER_KEY}"},
        json={"token": token, "host_uuid": server.uuid},
    )
    assert redeem2.status_code == 401, redeem2.text


def test_redeem_host_mismatch_rejected(client, db_session, location):
    integration = _make_integration(db_session)
    server = _make_server(db_session, location)
    service = _make_service(db_session, integration, server)

    mint = client.post(
        f"/api/billing/services/{service.id}/ipmi-ticket",
        headers={"Authorization": "Bearer key-1"},
    )
    token = mint.json()["launch_url"].split("t=", 1)[1]

    redeem = client.post(
        "/api/runner/ipmi/redeem",
        headers={"Authorization": f"Bearer {RUNNER_KEY}"},
        json={"token": token, "host_uuid": "some-other-uuid"},
    )
    assert redeem.status_code == 401, redeem.text
