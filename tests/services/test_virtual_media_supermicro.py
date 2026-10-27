"""SuperMicro virtual media: Redfish insert finished by the ATEN web UI mount_iso (no live BMC)."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.services.virtual_media import supermicro
from app.services.virtual_media.base import VirtualMediaStatus, VirtualMediaUnavailable

ORIGIN = "https://bmc.test"
ISO_URL = "http://runner.test:9083/isos/rf-boot-abc.iso"
ISO = "rf-boot-abc.iso"
VM_PAGE = '<script>SmcCsrfInsert ("CSRF-TOKEN", "tok123");</script>'


def _server():
    return SimpleNamespace(
        id=22,
        plugin_name="ipmi",
        plugin_config={"hostname": "bmc.test", "username": "ADMIN", "password": "pw"},
    )


class FakeBmc:
    """Redfish says 202 and never inserts (X11 01.74.14); the web UI mounts what Redfish saved."""

    def __init__(self):
        self.redfish_inserts_itself = False
        self.share_path = ""
        self.mounted = False
        self.mount_works = True
        self.has_web_page = True
        self.ops = []
        self.logouts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cgi/logout.cgi":
            self.logouts += 1
            return httpx.Response(200)
        if path == "/cgi/url_redirect.cgi":
            return httpx.Response(200, text=VM_PAGE) if self.has_web_page else httpx.Response(404)
        body = request.content.decode()
        op = body.split("&", 1)[0].removeprefix("op=")
        assert request.headers.get("CSRF-TOKEN") == "tok123"
        self.ops.append(op)
        if op == "vm_status":
            return httpx.Response(200, text=f'<IPMI><CODE NO="1" STATUS="{int(self.mounted)}"/></IPMI>')
        if op == "VIRTUAL_MEDIA_SHARE_IMAGE.XML":
            return httpx.Response(200, text=f'<IPMI><VM_INFO HOST="" PATH="{self.share_path}" USER="" PWD=""/></IPMI>')
        if op == "mount_iso":
            self.mounted = self.mount_works and bool(self.share_path)
            return httpx.Response(200, text="<IPMI><CODE NO=\"1\"/></IPMI>")
        if op == "umount_iso":
            self.mounted = False
            return httpx.Response(200, text="<IPMI><CODE NO=\"1\"/></IPMI>")
        return httpx.Response(404)

    async def insert_image(self, client, origin, image_url):
        self.share_path = image_url
        return VirtualMediaStatus(inserted=self.redfish_inserts_itself, device="CD1")

    async def read_status(self, client, origin):
        return VirtualMediaStatus(inserted=False, device="CD1")

    async def eject_image(self, client, origin):
        self.share_path = ""
        return VirtualMediaStatus(inserted=False, device="CD1")


@pytest.fixture
def fake_bmc(monkeypatch):
    bmc = FakeBmc()
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs.pop("verify", None)
        return real_client(*args, transport=httpx.MockTransport(bmc.handler), **kwargs)

    async def _login(origin, username, password):
        return "sid-1"

    monkeypatch.setattr(supermicro.httpx, "AsyncClient", _client)
    monkeypatch.setattr(supermicro, "aten_web_session", _login)
    monkeypatch.setattr(supermicro, "insert_image", bmc.insert_image)
    monkeypatch.setattr(supermicro, "read_status", bmc.read_status)
    monkeypatch.setattr(supermicro, "eject_image", bmc.eject_image)
    monkeypatch.setattr(supermicro, "MOUNT_POLL_SECONDS", 0)
    monkeypatch.setattr(supermicro, "MOUNT_WAIT_SECONDS", 0.2)
    return bmc


def test_parse_vm_status():
    assert supermicro.parse_vm_status('<IPMI><CODE NO="1" STATUS="1"/></IPMI>') is True
    assert supermicro.parse_vm_status('<IPMI><CODE NO="1" STATUS="0"/></IPMI>') is False
    assert supermicro.parse_vm_status('<IPMI><CODE NO="0" STATUS="1"/></IPMI>') is None
    assert supermicro.parse_vm_status("") is None


@pytest.mark.asyncio
async def test_insert_finishes_redfish_with_web_mount(fake_bmc):
    status = await supermicro.SuperMicroVirtualMediaProfile().insert(_server(), ISO_URL)
    assert status == VirtualMediaStatus(inserted=True, device="CD", image_name=ISO)
    assert "mount_iso" in fake_bmc.ops
    assert fake_bmc.mounted is True
    assert fake_bmc.logouts == 1


@pytest.mark.asyncio
async def test_insert_skips_web_mount_when_redfish_mounts(fake_bmc):
    fake_bmc.redfish_inserts_itself = True
    status = await supermicro.SuperMicroVirtualMediaProfile().insert(_server(), ISO_URL)
    assert status.inserted is True
    assert "mount_iso" not in fake_bmc.ops


@pytest.mark.asyncio
async def test_insert_unmounts_previous_web_mount_first(fake_bmc):
    fake_bmc.mounted = True
    fake_bmc.share_path = "http://old/old.iso"
    await supermicro.SuperMicroVirtualMediaProfile().insert(_server(), ISO_URL)
    assert fake_bmc.ops.index("umount_iso") < fake_bmc.ops.index("mount_iso")


@pytest.mark.asyncio
async def test_insert_raises_and_logs_out_when_mount_never_happens(fake_bmc):
    fake_bmc.mount_works = False
    with pytest.raises(VirtualMediaUnavailable, match="did not mount rf-boot-abc.iso"):
        await supermicro.SuperMicroVirtualMediaProfile().insert(_server(), ISO_URL)
    assert fake_bmc.logouts == 1


@pytest.mark.asyncio
async def test_insert_without_web_page_requires_redfish_mount(fake_bmc):
    fake_bmc.has_web_page = False
    with pytest.raises(VirtualMediaUnavailable, match="did not mount"):
        await supermicro.SuperMicroVirtualMediaProfile().insert(_server(), ISO_URL)
    assert "mount_iso" not in fake_bmc.ops


@pytest.mark.asyncio
async def test_status_reports_web_mount(fake_bmc):
    profile = supermicro.SuperMicroVirtualMediaProfile()
    assert (await profile.status(_server())).inserted is False
    fake_bmc.mounted = True
    fake_bmc.share_path = ISO_URL
    assert await profile.status(_server()) == VirtualMediaStatus(inserted=True, device="CD", image_name=ISO)


@pytest.mark.asyncio
async def test_eject_unmounts_web_mount(fake_bmc):
    fake_bmc.mounted = True
    fake_bmc.share_path = ISO_URL
    status = await supermicro.SuperMicroVirtualMediaProfile().eject(_server())
    assert status.inserted is False
    assert "umount_iso" in fake_bmc.ops
    assert fake_bmc.mounted is False
    assert fake_bmc.logouts == 1
