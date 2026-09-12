"""BMC virtual media: profiles, image tokens, orchestrator, permissions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

from app.core.client_permissions import PermissionKey
from app.core.config import settings
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus
from app.dao.user_dao import UserDAO
from app.services.virtual_media.base import VirtualMediaProfile, VirtualMediaStatus
from app.services.virtual_media.image_token import lookup_image_token
from app.services.virtual_media.registry import register_profile, unregister_profile


FAKE_PROFILE_ID = "test_vm"


class _FakeVirtualMediaProfile(VirtualMediaProfile):
    id = FAKE_PROFILE_ID
    display_name = "Test virtual media"

    def __init__(self):
        self.inserted = False
        self.last_url = ""

    async def status(self, server):
        del server
        name = self.last_url if self.inserted else ""
        return VirtualMediaStatus(inserted=self.inserted, image_name=name, device="CD")

    async def insert(self, server, image_url):
        del server
        self.last_url = image_url
        self.inserted = True
        return VirtualMediaStatus(inserted=True, image_name=image_url, device="CD")

    async def eject(self, server):
        del server
        self.inserted = False
        self.last_url = ""
        return VirtualMediaStatus(inserted=False, device="CD")


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


def _server(db_session, *, profile=FAKE_PROFILE_ID, name="vm-srv"):
    location = LocationDAO.create(db_session, name=f"loc-{name}")
    return ServerDAO.create(
        db_session,
        name=name,
        server_ip="10.16.251.140",
        plugin_name="ipmi",
        plugin_config={"hostname": "10.16.251.140", "username": "admin", "password": "secret"},
        location_id=location.id,
        virtual_media_profile=profile,
    )


def _bm_service(db_session, server, *, owner_user_id=None, permission_overrides=None, billing=False):
    kwargs = {
        "provisioning_source": ProvisioningSource.BILLING if billing else ProvisioningSource.INTERNAL,
        "status": ServiceStatus.ACTIVE,
    }
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="bm-vm",
        server_id=server.id,
        owner_user_id=owner_user_id,
        **kwargs,
    )
    if permission_overrides is not None:
        service.permission_overrides = permission_overrides
        ServiceDAO.update(db_session, service)
    return service


def _iso_catalog(tmp_path, monkeypatch, name="ubuntu.iso", payload=b"ISO-IMAGE-BYTES"):
    iso = tmp_path / name
    iso.write_bytes(payload)
    monkeypatch.setattr("app.services.virtual_media.iso_catalog._ISOS_DIR", tmp_path)
    return iso


def _token_from_url(image_url: str) -> str:
    parts = [p for p in urlparse(image_url).path.split("/") if p]
    return parts[-2]


def test_admin_lists_virtual_media_profiles(client, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    resp = client.get("/api/virtual-media/profiles", headers=headers)
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert {"asrockrack", "gigabyte", "supermicro"} <= ids


def test_create_server_rejects_unknown_virtual_media_profile(client, test_admin_user, db_session):
    headers = _login_admin(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-bad-vm")
    resp = client.post(
        "/api/servers/",
        headers=headers,
        json={
            "name": "bad-vm-profile",
            "server_ip": "10.9.9.7",
            "location_id": location.id,
            "plugin_name": "ipmi",
            "plugin_config": {"hostname": "10.9.9.7", "username": "admin", "password": "x"},
            "virtual_media_profile": "redfish",
        },
    )
    assert resp.status_code == 400, resp.text


def test_admin_status_409_without_profile(client, test_admin_user, db_session):
    headers = _login_admin(client, test_admin_user)
    server = _server(db_session, profile=None, name="vm-off")
    resp = client.get(f"/api/servers/{server.id}/virtual-media", headers=headers)
    assert resp.status_code == 409, resp.text


def test_insert_mints_path_token_eject_revokes_and_boot_once(
    client, test_admin_user, db_session, monkeypatch, tmp_path
):
    _iso_catalog(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "virtual_media_base_url", "http://bmc-fetch.test")
    profile = _FakeVirtualMediaProfile()
    register_profile(profile)
    plugin = SimpleNamespace(set_next_boot_device=AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.services.virtual_media.orchestrator.get_registry",
        lambda: SimpleNamespace(get_plugin=lambda *_a, **_k: plugin),
    )
    try:
        headers = _login_admin(client, test_admin_user)
        server = _server(db_session, name="vm-insert")
        inserted = client.post(
            f"/api/servers/{server.id}/virtual-media/insert",
            headers=headers,
            json={"filename": "ubuntu.iso", "boot_once": True},
        )
        assert inserted.status_code == 200, inserted.text
        body = inserted.json()
        assert body["inserted"] is True
        assert body["image_name"] == "ubuntu.iso"
        assert "http" not in (body["image_name"] or "")
        assert body["boot_once_applied"] is True
        plugin.set_next_boot_device.assert_awaited_once_with("cdrom", persistent=False)
        assert profile.last_url.startswith("http://bmc-fetch.test/api/virtual-media/images/")
        assert "?" not in profile.last_url
        token = _token_from_url(profile.last_url)
        assert lookup_image_token(token, "ubuntu.iso") is not None

        image = client.get(f"/api/virtual-media/images/{token}/ubuntu.iso")
        assert image.status_code == 200, image.text
        assert image.content == b"ISO-IMAGE-BYTES"

        ejected = client.post(f"/api/servers/{server.id}/virtual-media/eject", headers=headers)
        assert ejected.status_code == 200, ejected.text
        assert ejected.json()["inserted"] is False
        assert lookup_image_token(token, "ubuntu.iso") is None
        missing = client.get(f"/api/virtual-media/images/{token}/ubuntu.iso")
        assert missing.status_code == 401
    finally:
        unregister_profile(FAKE_PROFILE_ID)


def test_image_endpoint_head_range_and_traversal(client, db_session, monkeypatch, tmp_path):
    payload = b"ABCDEFGHIJ"
    _iso_catalog(tmp_path, monkeypatch, payload=payload)
    monkeypatch.setattr(settings, "virtual_media_base_url", "http://bmc-fetch.test")
    profile = _FakeVirtualMediaProfile()
    register_profile(profile)
    try:
        from app.services.virtual_media.image_token import mint_image_token

        server = _server(db_session, name="vm-range")
        token = mint_image_token(server.id, "ubuntu.iso")

        missing = client.get("/api/virtual-media/images/not-a-token/ubuntu.iso")
        assert missing.status_code == 401

        traversal = client.get(f"/api/virtual-media/images/{token}/..%2Fsecret.iso")
        assert traversal.status_code in (400, 404, 422)

        bad_name = client.get(f"/api/virtual-media/images/{token}/ubuntu.txt")
        assert bad_name.status_code == 400

        head = client.head(f"/api/virtual-media/images/{token}/ubuntu.iso")
        assert head.status_code == 200
        assert head.headers.get("accept-ranges") == "bytes"
        assert head.headers.get("content-length") == str(len(payload))
        assert head.content == b""

        ranged = client.get(
            f"/api/virtual-media/images/{token}/ubuntu.iso",
            headers={"Range": "bytes=2-5"},
        )
        assert ranged.status_code == 206
        assert ranged.content == b"CDEF"
        assert ranged.headers.get("content-range") == f"bytes 2-5/{len(payload)}"
    finally:
        unregister_profile(FAKE_PROFILE_ID)


def test_client_virtual_media_denied_without_permission(client, db_session, test_user, monkeypatch, tmp_path):
    _iso_catalog(tmp_path, monkeypatch)
    profile = _FakeVirtualMediaProfile()
    register_profile(profile)
    try:
        headers = _login_user(client, test_user)
        server = _server(db_session, name="vm-client-deny")
        service = _bm_service(
            db_session,
            server,
            owner_user_id=test_user.id,
            permission_overrides={PermissionKey.BMS_VIRTUAL_MEDIA: False},
        )
        resp = client.get(f"/api/services/{service.id}/virtual-media", headers=headers)
        assert resp.status_code == 403, resp.text
    finally:
        unregister_profile(FAKE_PROFILE_ID)


def test_billing_virtual_media_flag_and_permission(
    client, db_session, monkeypatch, tmp_path
):
    _iso_catalog(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "virtual_media_base_url", "http://bmc-fetch.test")
    profile = _FakeVirtualMediaProfile()
    register_profile(profile)
    try:
        integration = BillingIntegrationDAO.create(
            db_session, name="whmcs-vm", integration_type="whmcs"
        )
        key = integration.plaintext_api_key
        owner = UserDAO.create(
            db_session,
            username="vm-bill",
            email="vm-bill@example.com",
            billing_integration_id=integration.id,
            external_user_id="ext-vm-1",
            external_username="vm-bill",
            external_email="vm-bill@example.com",
        )
        server = _server(db_session, name="vm-bill-on")
        service = _bm_service(db_session, server, owner_user_id=owner.id, billing=True)
        headers = {"Authorization": f"Bearer {key}"}

        status_resp = client.get(f"/api/billing/services/{service.id}/status", headers=headers)
        assert status_resp.status_code == 200, status_resp.text
        assert status_resp.json()["virtual_media_available"] is True

        media = client.get(f"/api/billing/services/{service.id}/virtual-media", headers=headers)
        assert media.status_code == 200, media.text
        assert media.json()["inserted"] is False

        denied = _bm_service(
            db_session,
            server,
            owner_user_id=owner.id,
            billing=True,
            permission_overrides={PermissionKey.BMS_VIRTUAL_MEDIA: False},
        )
        forbidden = client.get(
            f"/api/billing/services/{denied.id}/virtual-media",
            headers=headers,
        )
        assert forbidden.status_code == 403, forbidden.text
        denied_status = client.get(
            f"/api/billing/services/{denied.id}/status",
            headers=headers,
        )
        assert denied_status.status_code == 200, denied_status.text
        assert denied_status.json()["virtual_media_available"] is False
    finally:
        unregister_profile(FAKE_PROFILE_ID)
