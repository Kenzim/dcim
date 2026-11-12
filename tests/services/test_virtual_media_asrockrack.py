"""ASRockRack virtual media over the MegaRAC web Remote Media API (no live BMC).

The fake BMC mirrors what firmware 1.83 (X470D4U2) returned when probed:
``/api/settings/media/general`` settings, the image list error while the share
is unreachable, and the ``configurations`` redirection list.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.services.virtual_media import asrockrack, gigabyte
from app.services.virtual_media.base import VirtualMediaStatus, VirtualMediaUnavailable
from app.services.virtual_media.registry import get_profile

ISO = "rfboot-abc.iso"
CIFS_URL = f"cifs://vm1234:tok-en_9@10.0.0.9/isos/{ISO}"
NFS_URL = f"nfs://10.0.0.9/srv/isos/{ISO}"

GENERAL = {
    "id": 1,
    "local_media_support": 0,
    "remote_media_support": 1,
    "same_settings": 1,
    "cd_remote_server_address": "10.16.251.200",
    "cd_remote_source_path": "/iso",
    "cd_remote_share_type": "nfs",
    "cd_remote_domain_name": "",
    "cd_remote_user_name": "",
    "mount_cd": 0,
}


def _server():
    return SimpleNamespace(
        ipmi_web_management_url="https://bmc.example",
        plugin_config={"username": "admin", "password": "secret"},
    )


class FakeBmc:
    def __init__(self, *, list_after_refreshes: int = 1, redirections=None, status_overrides=None):
        self.requests: list[tuple[str, str, object]] = []
        self.general = dict(GENERAL)
        self.refreshes = 0
        self.list_after_refreshes = list_after_refreshes
        self.redirections = list(redirections or [])
        self.status_overrides = status_overrides or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        if path in self.status_overrides:
            return httpx.Response(self.status_overrides[path], json={"error": "denied", "code": 1})
        if path == "/api/settings/media/general":
            if request.method == "PUT":
                self.general = body
            return httpx.Response(200, json=self.general)
        if path == "/api/settings/media/remote/rmediarefreshlist":
            self.refreshes += 1
            return httpx.Response(200, json={})
        if path == "/api/settings/media/remote/images":
            if self.refreshes < self.list_after_refreshes:
                return httpx.Response(500, json={"error": "Could not mount any of the remote path", "code": 1077})
            return httpx.Response(200, json=[{"image_name": ISO, "image_type": 1, "image_index": 0}])
        if path == "/api/settings/media/remote/configurations":
            return httpx.Response(200, json=self.redirections)
        if path == "/api/settings/media/remote/start-media":
            self.redirections = [
                {"media_type": 1, "media_index": body["image_index"], "image_name": body["image_name"], "redirection_status": 1}
            ]
            return httpx.Response(200, json={})
        if path == "/api/settings/media/remote/stop-media":
            self.redirections = [dict(r, redirection_status=0) for r in self.redirections]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": "Invalid API Call", "code": 1010})

    def calls(self, method: str, path: str) -> list:
        return [body for m, p, body in self.requests if m == method and p == path]


@pytest.fixture
def session_calls(monkeypatch):
    calls = []

    async def fake_login(origin, username, password):
        calls.append(("login", origin))
        return "cookie1", "csrf1", {}

    async def fake_logout(origin, cookie, csrf):
        calls.append(("logout", origin, cookie, csrf))

    monkeypatch.setattr(asrockrack, "megarac_web_session", fake_login)
    monkeypatch.setattr(asrockrack, "megarac_logout", fake_logout)
    monkeypatch.setattr(asrockrack, "IMAGE_POLL_SECONDS", 0)
    return calls


@pytest.fixture
def fake_bmc(monkeypatch):
    bmc = FakeBmc()
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return real_client(transport=httpx.MockTransport(bmc.handler), **kwargs)

    monkeypatch.setattr(asrockrack.httpx, "AsyncClient", client_factory)
    return bmc


def test_profile_flags():
    assert get_profile("asrockrack").mounts_nfs is True
    assert get_profile("asrockrack").mounts_cifs is False
    assert get_profile("gigabyte").mounts_cifs is False
    assert get_profile("gigabyte").mounts_nfs is True
    assert get_profile("supermicro_x9").mounts_cifs is True


def test_share_target_parses_nfs_url():
    assert asrockrack.share_target(NFS_URL) == ("nfs", "10.0.0.9", "/srv/isos", ISO, "", "")


def test_share_target_rejects_nfs_url_without_export():
    with pytest.raises(VirtualMediaUnavailable, match="export"):
        asrockrack.share_target(f"nfs://10.0.0.9/{ISO}")


@pytest.mark.asyncio
async def test_insert_nfs_points_cd_at_export(session_calls, fake_bmc):
    status = await asrockrack.AsrockRackVirtualMediaProfile().insert(_server(), NFS_URL)

    assert status == VirtualMediaStatus(inserted=True, device="CD", image_name=ISO)
    (put,) = fake_bmc.calls("PUT", "/api/settings/media/general")
    assert put["mount_cd"] == 1
    assert put["same_settings"] == 0
    assert put["cd_remote_share_type"] == "nfs"
    assert put["cd_remote_server_address"] == "10.0.0.9"
    assert put["cd_remote_source_path"] == "/srv/isos"
    assert put["cd_remote_user_name"] == ""
    assert fake_bmc.calls("POST", "/api/settings/media/remote/start-media") == [
        {"image_name": ISO, "image_type": 1, "image_index": 0}
    ]
    assert session_calls[-1][0] == "logout"


def test_cifs_target_splits_share_and_file():
    assert asrockrack.cifs_target(CIFS_URL) == ("10.0.0.9", "/isos", ISO, "vm1234", "tok-en_9")


def test_cifs_target_rejects_http_url_with_runner_hint():
    with pytest.raises(VirtualMediaUnavailable, match="needs a connected media runner"):
        asrockrack.cifs_target("http://app/api/virtual-media/images/x/rfboot.iso")


def test_cifs_target_rejects_password_the_bmc_refuses():
    with pytest.raises(VirtualMediaUnavailable, match="characters the BMC rejects"):
        asrockrack.cifs_target("cifs://u:a%2Cb@10.0.0.9/isos/x.iso")


@pytest.mark.asyncio
async def test_insert_points_cd_at_share_then_starts_redirection(session_calls, fake_bmc):
    status = await asrockrack.AsrockRackVirtualMediaProfile().insert(_server(), CIFS_URL)

    assert status == VirtualMediaStatus(inserted=True, device="CD", image_name=ISO)
    (put,) = fake_bmc.calls("PUT", "/api/settings/media/general")
    assert put["mount_cd"] == 1
    assert put["cd_remote_share_type"] == "cifs"
    assert put["cd_remote_server_address"] == "10.0.0.9"
    assert put["cd_remote_source_path"] == "/isos"
    assert put["cd_remote_user_name"] == "vm1234"
    assert put["cd_remote_password"] == "tok-en_9"
    assert put["same_settings"] == 0  # CD settings only; FD/HD keep their own
    assert fake_bmc.calls("POST", "/api/settings/media/remote/rmediarefreshlist")[0] == {"appType": 1, "mediaType": 1}
    assert fake_bmc.calls("POST", "/api/settings/media/remote/start-media") == [
        {"image_name": ISO, "image_type": 1, "image_index": 0}
    ]
    assert session_calls[-1][0] == "logout"


@pytest.mark.asyncio
async def test_insert_waits_for_share_listing(session_calls, fake_bmc):
    fake_bmc.list_after_refreshes = 3
    await asrockrack.AsrockRackVirtualMediaProfile().insert(_server(), CIFS_URL)
    assert fake_bmc.refreshes == 3


@pytest.mark.asyncio
async def test_insert_stops_previous_redirection_first(session_calls, fake_bmc):
    fake_bmc.redirections = [{"media_type": 1, "media_index": 0, "image_name": "old.iso", "redirection_status": 1}]
    await asrockrack.AsrockRackVirtualMediaProfile().insert(_server(), CIFS_URL)
    assert fake_bmc.calls("POST", "/api/settings/media/remote/stop-media") == [
        {"image_name": "old.iso", "image_type": 1, "image_index": 0}
    ]
    order = [p for m, p, _ in fake_bmc.requests if m in ("POST", "PUT")]
    assert order.index("/api/settings/media/remote/stop-media") < order.index("/api/settings/media/general")


@pytest.mark.asyncio
async def test_insert_reports_share_error_when_image_never_listed(session_calls, fake_bmc, monkeypatch):
    fake_bmc.list_after_refreshes = 10**6
    monkeypatch.setattr(asrockrack, "IMAGE_WAIT_SECONDS", 0)
    with pytest.raises(VirtualMediaUnavailable) as err:
        await asrockrack.AsrockRackVirtualMediaProfile().insert(_server(), CIFS_URL)
    assert err.value.detail == (
        f"BMC did not list {ISO} on the remote share: Could not mount any of the remote path (code 1077)"
    )
    assert not fake_bmc.calls("POST", "/api/settings/media/remote/start-media")
    assert session_calls[-1][0] == "logout"


@pytest.mark.asyncio
async def test_insert_refuses_when_remote_media_disabled(session_calls, fake_bmc):
    fake_bmc.general["remote_media_support"] = 0
    with pytest.raises(VirtualMediaUnavailable, match="Remote media is disabled"):
        await asrockrack.AsrockRackVirtualMediaProfile().insert(_server(), CIFS_URL)
    assert not fake_bmc.calls("PUT", "/api/settings/media/general")


@pytest.mark.asyncio
async def test_web_api_auth_failure_is_reported(session_calls, fake_bmc):
    fake_bmc.status_overrides = {"/api/settings/media/remote/configurations": 401}
    with pytest.raises(VirtualMediaUnavailable) as err:
        await asrockrack.AsrockRackVirtualMediaProfile().status(_server())
    assert err.value.detail == (
        "BMC web session was rejected (HTTP 401 on /api/settings/media/remote/configurations)"
    )
    assert session_calls[-1][0] == "logout"


@pytest.mark.asyncio
async def test_bmc_error_body_is_included(session_calls, fake_bmc):
    fake_bmc.status_overrides = {"/api/settings/media/general": 500}
    with pytest.raises(VirtualMediaUnavailable, match=r"BMC refused /api/settings/media/general \(HTTP 500: denied \(code 1\)\)"):
        await asrockrack.AsrockRackVirtualMediaProfile().insert(_server(), CIFS_URL)


@pytest.mark.asyncio
async def test_status_and_eject(session_calls, fake_bmc):
    profile = asrockrack.AsrockRackVirtualMediaProfile()
    assert (await profile.status(_server())).inserted is False

    fake_bmc.redirections = [{"media_type": 1, "media_index": 0, "image_name": ISO, "redirection_status": 1}]
    assert await profile.status(_server()) == VirtualMediaStatus(inserted=True, device="CD", image_name=ISO)

    status = await profile.eject(_server())
    assert status.inserted is False
    assert fake_bmc.calls("POST", "/api/settings/media/remote/stop-media") == [
        {"image_name": ISO, "image_type": 1, "image_index": 0}
    ]
    assert [c[0] for c in session_calls].count("logout") == 3


@pytest.mark.asyncio
async def test_eject_with_nothing_mounted_is_a_no_op(session_calls, fake_bmc):
    status = await asrockrack.AsrockRackVirtualMediaProfile().eject(_server())
    assert status.inserted is False
    assert not fake_bmc.calls("POST", "/api/settings/media/remote/stop-media")


@pytest.mark.asyncio
async def test_gigabyte_uses_redfish_basic_auth_without_web_session(session_calls, monkeypatch):
    seen = {}

    async def failing_insert(client, origin, image_url):
        seen["auth"] = client.auth
        raise VirtualMediaUnavailable("BMC Redfish rejected the credentials (HTTP 401 on /redfish/v1/Managers)")

    monkeypatch.setattr(gigabyte, "insert_image", failing_insert)
    with pytest.raises(VirtualMediaUnavailable, match="HTTP 401"):
        await gigabyte.GigabyteVirtualMediaProfile().insert(_server(), "nfs://10.0.0.9/srv/isos/x.iso")
    assert isinstance(seen["auth"], httpx.BasicAuth)
    assert session_calls == []


def _gigabyte_redfish(options: list[dict], patches: list):
    boot = {
        "BootOptions": {"@odata.id": "/redfish/v1/Systems/Self/BootOptions"},
        "BootSourceOverrideTarget@Redfish.AllowableValues": ["None", "Pxe", "Cd", "Hdd", "UefiBootNext"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PATCH":
            patches.append((path, json.loads(request.content), request.headers.get("if-match")))
            return httpx.Response(204)
        if path == "/redfish/v1/Systems":
            return httpx.Response(200, json={"Members": [{"@odata.id": "/redfish/v1/Systems/Self"}]})
        if path == "/redfish/v1/Systems/Self":
            return httpx.Response(200, json={"Boot": boot}, headers={"ETag": 'W/"1"'})
        if path == "/redfish/v1/Systems/Self/BootOptions":
            return httpx.Response(200, json={"Members": [{"@odata.id": f"{path}/{o['Id']}"} for o in options]})
        for option in options:
            if path.endswith("/" + option["Id"]):
                return httpx.Response(200, json=option)
        return httpx.Response(404)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session(_server):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield client, "https://bmc.example"

    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options,expected",
    [
        (
            [
                {"Id": "0014", "BootOptionReference": "Boot0014", "DisplayName": "UEFI: PXE IPv4 Intel(R) I350"},
                {"Id": "0017", "BootOptionReference": "Boot0017", "DisplayName": "UEFI: AMI Virtual CDROM0 1.00, Partition 1"},
            ],
            {"BootSourceOverrideTarget": "UefiBootNext", "BootNext": "Boot0017"},
        ),
        ([{"Id": "0014", "BootOptionReference": "Boot0014", "DisplayName": "UEFI: PXE IPv4"}], {"BootSourceOverrideTarget": "Cd"}),
    ],
)
async def test_gigabyte_boot_once_targets_the_virtual_cdrom_entry(monkeypatch, options, expected):
    patches: list = []
    profile = gigabyte.GigabyteVirtualMediaProfile()
    monkeypatch.setattr(profile, "_session", _gigabyte_redfish(options, patches))
    assert await profile.set_boot_once(_server(), uefi=True) is True
    (path, body, if_match), = patches
    assert path == "/redfish/v1/Systems/Self" and if_match == 'W/"1"'
    assert body["Boot"] == {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideMode": "UEFI", **expected}
