"""A server's stored OS credentials never reach another order's payload.

Covers the install paths (replace, not merge), termination (clears the column
only for the server's current live service), and the service payload (only the
current install's keys, and nothing for a terminated or superseded service).
"""
import asyncio
from types import SimpleNamespace

import pytest

import app.api.billing as billing
import app.api.server_interaction as interaction
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from tests.api.test_runner_proxy_lifecycle import _admin_headers

# The credentials helpers live in a module the fix adds; the tests also run without it.
try:
    import app.services.server_credentials as server_credentials
except ImportError:
    server_credentials = None

UBUNTU_KEYS = {"username", "password", "ssh_public_key"}
WINDOWS_KEYS = {"admin_password"}
META_KEYS = {"os_type", "template_id", "last_updated"}


def _template(ident, os_type, keys):
    return SimpleNamespace(
        id=ident,
        name=ident,
        os_type=os_type,
        disk_image=None,
        template_dir=None,
        parameters={
            k: SimpleNamespace(type="text", required=False, default=None, generate=None)
            for k in keys
        },
    )


UBUNTU = _template("ubuntu-cloud-image", "linux", UBUNTU_KEYS)
WINDOWS = _template("windows-server-2022", "windows", WINDOWS_KEYS)
TEMPLATES = {UBUNTU.id: UBUNTU, WINDOWS.id: WINDOWS}

UBUNTU_PARAMS = {"username": "rackflow", "password": "ubuntu-secret", "ssh_public_key": "ssh-ed25519 AAAA"}
WINDOWS_PARAMS = {"admin_password": "windows-secret"}


@pytest.fixture(autouse=True)
def _install_mocks(monkeypatch, tmp_path):
    script = tmp_path / "install.sh"
    script.write_text("echo install", encoding="utf-8")
    templates = SimpleNamespace(
        get_template=lambda ident: TEMPLATES.get(ident),
        get_template_script_path=lambda ident: script,
        enumerate_relative_files=lambda ident: [],
    )
    temp_os = SimpleNamespace(
        get_kernel_url=lambda _i, b: f"{b}/k",
        get_initrd_url=lambda _i, b: f"{b}/i",
        get_kernel_params=lambda _i: "quiet",
        get_squashfs_url=lambda _i, b: f"{b}/s",
    )
    tokens = SimpleNamespace(generate_token=lambda **kw: "tok")
    if server_credentials is not None:
        monkeypatch.setattr(server_credentials, "get_template_service", lambda: templates)
    for module in (billing, interaction):
        monkeypatch.setattr(module, "get_template_service", lambda: templates)
        monkeypatch.setattr(module, "get_temp_os_service", lambda: temp_os)
        monkeypatch.setattr(module, "get_download_token_service", lambda: tokens)
        monkeypatch.setattr(module, "_get_base_url_for_pxe_ip", lambda *_: "https://pxe.test")
        monkeypatch.setattr(module, "schedule_launch", lambda *a, **k: None)
    original = billing.BootTaskDAO.create

    def create(*args, **kwargs):
        kwargs.pop("status", None)
        return original(*args, **kwargs)

    monkeypatch.setattr(billing.BootTaskDAO, "create", create)
    monkeypatch.setattr(billing.InstallationTaskDAO, "get_active_by_server", lambda *_: None)
    monkeypatch.setattr(billing.NetworkPortDAO, "get_pxe_boot_port", lambda *_: None)
    monkeypatch.setattr(billing.DiskDAO, "get_os_disk", lambda *_: None)


def _server(db, name, ip):
    loc = LocationDAO.create(db, name=f"{name}-loc")
    return ServerDAO.create(
        db, name=name, server_ip=ip, plugin_name="ipmi", plugin_config={}, location_id=loc.id
    )


def _user(db, integration, name):
    return UserDAO.create(
        db,
        username=name,
        email=f"{name}@example.test",
        billing_integration_id=integration.id,
        external_user_id=name,
    )


def _service(db, server, owner, name, status=ServiceStatus.ACTIVE):
    return ServiceDAO.create_bare_metal(
        db,
        name=name,
        server_id=server.id,
        owner_user_id=owner.id,
        service_type=ServiceType.BARE_METAL,
        status=status,
        provisioning_source=ProvisioningSource.BILLING,
        config={},
    )


def _stored(db, server):
    db.refresh(server)
    return server.credentials or {}


def _billing_install(db, service, template_id, params):
    billing._queue_template_install_for_service(db, service, template_id, params)


def _admin_install(db, server, template_id, params):
    body = interaction.BootTaskCreate(
        template_id=template_id, template_parameters=params, launch=False
    )
    asyncio.run(interaction.create_boot_task(server.id, body, {"user_id": 1}, db))


def test_billing_install_replaces_credentials_ubuntu_then_windows(db_session):
    integration = BillingIntegrationDAO.create(db_session, name="cred-a", integration_type="whmcs")
    server = _server(db_session, "cred-a-srv", "198.51.100.10")
    service = _service(db_session, server, _user(db_session, integration, "cred-a-u"), "cred-a-svc")
    _billing_install(db_session, service, UBUNTU.id, dict(UBUNTU_PARAMS))
    assert set(_stored(db_session, server)) == META_KEYS | UBUNTU_KEYS
    _billing_install(db_session, service, WINDOWS.id, dict(WINDOWS_PARAMS))
    stored = _stored(db_session, server)
    assert set(stored) == META_KEYS | WINDOWS_KEYS
    assert stored["os_type"] == "windows"
    assert stored["template_id"] == WINDOWS.id


def test_billing_install_replaces_credentials_windows_then_ubuntu(db_session):
    integration = BillingIntegrationDAO.create(db_session, name="cred-b", integration_type="whmcs")
    server = _server(db_session, "cred-b-srv", "198.51.100.11")
    service = _service(db_session, server, _user(db_session, integration, "cred-b-u"), "cred-b-svc")
    _billing_install(db_session, service, WINDOWS.id, dict(WINDOWS_PARAMS))
    assert set(_stored(db_session, server)) == META_KEYS | WINDOWS_KEYS
    _billing_install(db_session, service, UBUNTU.id, dict(UBUNTU_PARAMS))
    stored = _stored(db_session, server)
    assert set(stored) == META_KEYS | UBUNTU_KEYS
    assert "admin_password" not in stored


def test_admin_boot_task_install_replaces_credentials(db_session):
    server = _server(db_session, "cred-c-srv", "198.51.100.12")
    _admin_install(db_session, server, UBUNTU.id, dict(UBUNTU_PARAMS))
    assert set(_stored(db_session, server)) == META_KEYS | UBUNTU_KEYS
    _admin_install(db_session, server, WINDOWS.id, dict(WINDOWS_PARAMS))
    assert set(_stored(db_session, server)) == META_KEYS | WINDOWS_KEYS
    _admin_install(db_session, server, UBUNTU.id, dict(UBUNTU_PARAMS))
    assert set(_stored(db_session, server)) == META_KEYS | UBUNTU_KEYS


DIRTY = {
    "os_type": "windows",
    "template_id": WINDOWS.id,
    "last_updated": "2026-01-01T00:00:00",
    "admin_password": "windows-secret",
    "username": "previous-customer",
    "password": "previous-customer-password",
    "ssh_public_key": "ssh-ed25519 PREVIOUS",
    "something_else": "x",
}


def _payload_world(db, name, ip, credentials):
    integration = BillingIntegrationDAO.create(db, name=name, integration_type="whmcs")
    owner = _user(db, integration, f"{name}-u")
    server = _server(db, f"{name}-srv", ip)
    server.credentials = credentials
    db.commit()
    service = _service(db, server, owner, f"{name}-svc")
    return integration, owner, server, service


def test_payload_carries_only_current_template_keys_from_dirty_column(db_session):
    _, _, _, service = _payload_world(db_session, "pay-a", "198.51.100.20", dict(DIRTY))
    creds = billing._billing_service_response(db_session, service).credentials
    assert set(creds) == META_KEYS | WINDOWS_KEYS
    assert creds["os_type"] == "windows"


def test_payload_for_windows_order_on_previously_ubuntu_server_has_no_login_keys(db_session):
    _, _, _, service = _payload_world(db_session, "pay-b", "198.51.100.21", dict(DIRTY))
    creds = billing._billing_service_response(db_session, service).model_dump()["credentials"]
    assert "username" not in creds
    assert "password" not in creds
    assert "ssh_public_key" not in creds


def test_payload_for_ubuntu_order_on_previously_windows_server(db_session):
    dirty = {**DIRTY, "os_type": "linux", "template_id": UBUNTU.id}
    _, _, _, service = _payload_world(db_session, "pay-c", "198.51.100.22", dirty)
    creds = billing._billing_service_response(db_session, service).credentials
    assert set(creds) == META_KEYS | UBUNTU_KEYS
    assert "admin_password" not in creds


def test_payload_unknown_template_returns_only_metadata(db_session):
    dirty = {**DIRTY, "template_id": "removed-template"}
    _, _, _, service = _payload_world(db_session, "pay-e", "198.51.100.24", dirty)
    creds = billing._billing_service_response(db_session, service).credentials
    assert set(creds) <= META_KEYS
    assert creds["os_type"] == "windows"


def test_payload_without_stored_credentials_is_null(db_session):
    _, _, _, service = _payload_world(db_session, "pay-f", "198.51.100.25", None)
    assert billing._billing_service_response(db_session, service).credentials is None


def _resold_world(db, name, ip):
    """An earlier customer's terminated service, then the same server sold again."""
    integration, owner, server, old = _payload_world(db, name, ip, dict(DIRTY))
    old.status = ServiceStatus.TERMINATED
    db.commit()
    new = _service(db, server, _user(db, integration, f"{name}-new"), f"{name}-new-svc")
    return integration, server, old, new


def test_terminated_service_payload_has_no_server_data_after_resale(db_session):
    _, server, old, new = _resold_world(db_session, "term-a", "198.51.100.30")
    gone = billing._billing_service_response(db_session, old)
    assert gone.credentials is None
    assert gone.server_ip is None
    live = billing._billing_service_response(db_session, new)
    assert live.credentials is not None
    assert live.server_ip == server.server_ip


def test_reseller_style_service_payload_for_terminated_service(db_session):
    from app.api.reseller import _service_payload

    _, _, old, new = _resold_world(db_session, "term-b", "198.51.100.31")
    gone = _service_payload(db_session, old)
    assert gone["credentials"] is None
    assert gone["server_ip"] is None
    assert set(_service_payload(db_session, new)["credentials"]) == META_KEYS | WINDOWS_KEYS


def test_stale_non_terminated_link_gets_no_credentials(db_session):
    integration, _, server, first = _payload_world(db_session, "term-c", "198.51.100.32", dict(DIRTY))
    second = _service(db_session, server, _user(db_session, integration, "term-c-2"), "term-c-2-svc")
    for service in (first, second):
        payload = billing._billing_service_response(db_session, service)
        assert payload.credentials is None
        assert payload.server_ip is None


def _headers(integration):
    return {"Authorization": f"Bearer {integration.plaintext_api_key}"}


def test_billing_list_and_get_routes_hide_server_data_of_terminated_service(client, db_session):
    integration, server, old, new = _resold_world(db_session, "term-d", "198.51.100.33")
    listing = client.get("/api/billing/services", headers=_headers(integration))
    assert listing.status_code == 200, listing.text
    by_id = {row["id"]: row for row in listing.json()}
    assert by_id[old.id]["credentials"] is None
    assert by_id[old.id]["server_ip"] is None
    assert set(by_id[new.id]["credentials"]) == META_KEYS | WINDOWS_KEYS
    detail = client.get(f"/api/billing/services/{old.id}", headers=_headers(integration))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["credentials"] is None
    assert body["server_ip"] is None
    assert body["server"] is None
    assert "windows-secret" not in detail.text
    assert "previous-customer-password" not in detail.text
    assert server.name not in detail.text
    live = client.get(f"/api/billing/services/{new.id}", headers=_headers(integration)).json()
    assert live["server"]["name"] == server.name


# Termination: three paths, each must clear only for the server's current live service.

def _terminate_billing(client, db, integration, service):
    resp = client.delete(f"/api/billing/services/{service.id}", headers=_headers(integration))
    assert resp.status_code == 204, resp.text


def _terminate_admin(client, db, integration, service, admin):
    resp = client.put(
        f"/api/admin/services/{service.id}/status",
        headers=_admin_headers(client, admin),
        json={"status": "terminated"},
    )
    assert resp.status_code == 200, resp.text


def _terminate_commerce(client, db, integration, service, admin):
    commerce = pytest.importorskip("app.services.commerce_lifecycle_service")
    terminate_service = getattr(commerce, "terminate_service", None)
    if terminate_service is None:
        pytest.skip("this branch has no commerce terminate_service")

    service.status = ServiceStatus.SUSPENDED if service.status != ServiceStatus.TERMINATED else service.status
    db.commit()
    asyncio.run(terminate_service(db, service))
    db.commit()


@pytest.fixture(params=["billing", "admin", "commerce"])
def terminate(request, client, db_session, test_admin_user):
    def run(integration, service):
        if request.param == "billing":
            _terminate_billing(client, db_session, integration, service)
        elif request.param == "admin":
            _terminate_admin(client, db_session, integration, service, test_admin_user)
        else:
            _terminate_commerce(client, db_session, integration, service, test_admin_user)

    return run


def test_terminating_current_live_service_clears_credentials(db_session, terminate):
    integration, _, server, service = _payload_world(db_session, "tc-a", "198.51.100.40", dict(DIRTY))
    terminate(integration, service)
    assert not _stored(db_session, server)


def test_terminating_again_does_not_touch_new_occupants_credentials(db_session, terminate):
    integration, server, old, new = _resold_world(db_session, "tc-b", "198.51.100.41")
    server.credentials = {**DIRTY, "os_type": "linux", "template_id": UBUNTU.id}
    db_session.commit()
    terminate(integration, old)
    assert set(_stored(db_session, server)) >= META_KEYS | UBUNTU_KEYS
    assert new.status != ServiceStatus.TERMINATED


def test_terminating_superseded_live_service_keeps_credentials(db_session, terminate):
    integration, _, server, first = _payload_world(db_session, "tc-c", "198.51.100.42", dict(DIRTY))
    _service(db_session, server, _user(db_session, integration, "tc-c-2"), "tc-c-2-svc")
    terminate(integration, first)
    assert _stored(db_session, server)
