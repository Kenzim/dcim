"""The megarac_stream virtual-media profile (no BMC, no Redis)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.ipmi_kvm import cd_stream
from app.services.ipmi_kvm.cd_stream import CdStreamError, CdStreamInfo
from app.services.virtual_media import megarac_stream
from app.services.virtual_media.base import VirtualMediaUnavailable
from app.services.virtual_media.gigabyte import GigabyteVirtualMediaProfile
from app.services.virtual_media.megarac_stream import MegaracStreamVirtualMediaProfile, image_filename
from app.services.virtual_media.registry import get_profile, list_profiles


def _server(kvm_profile: str = "asrockrack", sid: int = 4):
    return SimpleNamespace(
        id=sid,
        virtual_media_profile="megarac_stream",
        ipmi_kvm_profile=kvm_profile,
        ipmi_web_management_url="https://bmc.example",
        plugin_config={"username": "admin", "password": "secret"},
    )


class FakeManager:
    def __init__(self):
        self.started = []
        self.stopped = []
        self.current = None
        self.error = None

    async def start(self, server_id, target, url, filename):
        if self.error:
            raise self.error
        self.started.append((server_id, target, url, filename))
        self.current = CdStreamInfo(state=cd_stream.STATE_SERVING, filename=filename, url=url)
        return self.current

    async def stop(self, server_id):
        self.stopped.append(server_id)
        self.current = None
        return True

    def info(self, server_id):
        return self.current


@pytest.fixture
def manager(monkeypatch):
    fake = FakeManager()
    monkeypatch.setattr(megarac_stream, "get_manager", lambda: fake)
    return fake


def test_profile_is_registered_and_needs_no_share():
    profile = get_profile("megarac_stream")
    assert isinstance(profile, MegaracStreamVirtualMediaProfile)
    assert profile.streams_iso and not profile.mounts_nfs and not profile.mounts_cifs
    assert "megarac_stream" in {row["id"] for row in list_profiles()}
    assert not getattr(get_profile("gigabyte"), "streams_iso", False)


@pytest.mark.parametrize(
    "url,name",
    [
        ("file:///srv/isos/alpine.iso", "alpine.iso"),
        ("rfboot://rfboot-abc.iso", "rfboot-abc.iso"),
        ("http://runner:8080/isos/My%20Disc.iso", "My Disc.iso"),
    ],
)
def test_image_filename(url, name):
    assert image_filename(url) == name


def test_image_filename_requires_a_name():
    with pytest.raises(VirtualMediaUnavailable):
        image_filename("")


@pytest.mark.asyncio
async def test_insert_starts_the_stream_with_the_bmc_credentials(manager):
    status = await MegaracStreamVirtualMediaProfile().insert(_server(), "rfboot://rfboot-abc.iso")
    assert status.inserted and status.image_name == "rfboot-abc.iso"
    server_id, target, url, filename = manager.started[0]
    assert (server_id, url, filename) == (4, "rfboot://rfboot-abc.iso", "rfboot-abc.iso")
    assert (target.origin, target.host, target.username, target.password) == (
        "https://bmc.example", "bmc.example", "admin", "secret",
    )


@pytest.mark.asyncio
async def test_insert_reports_a_refused_redirection(manager):
    manager.error = CdStreamError("The BMC virtual CD is in use (another remote console is redirecting media)")
    with pytest.raises(VirtualMediaUnavailable, match="in use"):
        await MegaracStreamVirtualMediaProfile().insert(_server(), "file:///a.iso")


@pytest.mark.asyncio
async def test_status_and_eject_follow_the_stream(manager):
    profile = MegaracStreamVirtualMediaProfile()
    assert (await profile.status(_server())).inserted is False
    await profile.insert(_server(), "file:///a.iso")
    status = await profile.status(_server())
    assert status.inserted and status.image_name == "a.iso"
    assert (await profile.eject(_server())).inserted is False
    assert manager.stopped == [4]
    assert (await profile.status(_server())).inserted is False


@pytest.mark.asyncio
async def test_a_failed_stream_is_not_reported_as_inserted(manager):
    manager.current = CdStreamInfo(state=cd_stream.STATE_FAILED, filename="a.iso", url="file:///a.iso")
    assert (await MegaracStreamVirtualMediaProfile().status(_server())).inserted is False


@pytest.mark.asyncio
async def test_asrock_boot_once_defers_to_the_ipmi_override(monkeypatch):
    async def redfish(self, server, uefi):
        raise AssertionError("ASRock Rack must not use Redfish BootNext")

    monkeypatch.setattr(GigabyteVirtualMediaProfile, "set_boot_once", redfish)
    assert await MegaracStreamVirtualMediaProfile().set_boot_once(_server("asrockrack"), uefi=True) is None


@pytest.mark.asyncio
async def test_gigabyte_boot_once_uses_redfish_bootnext(monkeypatch):
    calls = []

    async def redfish(self, server, uefi):
        calls.append(uefi)
        return True

    monkeypatch.setattr(GigabyteVirtualMediaProfile, "set_boot_once", redfish)
    assert await MegaracStreamVirtualMediaProfile().set_boot_once(_server("gigabyte"), uefi=True) is True
    assert calls == [True]


@pytest.mark.asyncio
async def test_orchestrator_hands_streaming_profiles_the_local_catalog_file(tmp_path, monkeypatch):
    from app.services.virtual_media import orchestrator

    iso = tmp_path / "a.iso"
    iso.write_bytes(b"x")
    monkeypatch.setattr(orchestrator, "iso_path", lambda name: tmp_path / name)
    profile = MegaracStreamVirtualMediaProfile()
    assert await orchestrator._image_url_for_insert(None, _server(), profile, "a.iso") == iso.as_uri()
    with pytest.raises(VirtualMediaUnavailable, match="not found"):
        await orchestrator._image_url_for_insert(None, _server(), profile, "missing.iso")


@pytest.mark.asyncio
async def test_orchestrator_falls_back_to_the_runner_library_over_http(tmp_path, monkeypatch):
    from app.services.virtual_media import orchestrator

    monkeypatch.setattr(orchestrator, "iso_path", lambda name: tmp_path / name)
    runner = SimpleNamespace(id=1, state={"public_http_base": "http://runner.example:8080"})
    monkeypatch.setattr(
        "app.dao.runner_dao.RunnerDAO.get_by_location_and_capability", staticmethod(lambda *_a, **_k: runner)
    )
    monkeypatch.setattr("app.services.runners.hub.get_hub", lambda: SimpleNamespace(is_connected=lambda _id: True))
    monkeypatch.setattr("app.services.runners.cache.cached_state", lambda _id: runner.state)
    monkeypatch.setattr(
        "app.services.runners.media_urls.public_http_base_from_state", lambda state: state["public_http_base"]
    )
    monkeypatch.setattr(
        "app.services.runners.media_urls.media_http_iso_url", lambda base, name: f"{base}/isos/{name}"
    )
    server = _server()
    server.location_id = 3
    url = await orchestrator._image_url_for_insert(None, server, MegaracStreamVirtualMediaProfile(), "a.iso")
    assert url == "http://runner.example:8080/isos/a.iso"


# ---------------------------------------------------------------- resume after restart


class FakeResumeManager(FakeManager):
    def __init__(self, records, claimable=True):
        super().__init__()
        self._records = records
        self.claimable = claimable
        self.forgotten = []
        self.released = []

    def records(self):
        return self._records

    def claim(self, server_id):
        return self.claimable

    def forget(self, server_id):
        self.forgotten.append(server_id)

    def release_claim(self, server_id):
        self.released.append(server_id)


def _patch_server_lookup(monkeypatch, server):
    class Session:
        def close(self):
            return None

    monkeypatch.setattr("app.core.database.SessionLocal", lambda: Session())
    monkeypatch.setattr("app.dao.server_dao.ServerDAO.get_by_id", staticmethod(lambda _db, _id: server))


@pytest.mark.asyncio
async def test_resume_restarts_a_recorded_stream(monkeypatch):
    fake = FakeResumeManager([(4, {"url": "file:///a.iso", "filename": "a.iso"})])
    monkeypatch.setattr(megarac_stream, "get_manager", lambda: fake)
    _patch_server_lookup(monkeypatch, _server())
    await megarac_stream.resume_streams()
    assert [(s[0], s[2], s[3]) for s in fake.started] == [(4, "file:///a.iso", "a.iso")]


@pytest.mark.asyncio
async def test_resume_drops_records_of_servers_that_changed_profile(monkeypatch):
    fake = FakeResumeManager([(4, {"url": "file:///a.iso", "filename": "a.iso"})])
    monkeypatch.setattr(megarac_stream, "get_manager", lambda: fake)
    server = _server()
    server.virtual_media_profile = "gigabyte"
    _patch_server_lookup(monkeypatch, server)
    await megarac_stream.resume_streams()
    assert fake.started == [] and fake.forgotten == [4]


@pytest.mark.asyncio
async def test_resume_waits_while_the_previous_owner_is_still_live(monkeypatch):
    fake = FakeResumeManager([(4, {"url": "file:///a.iso", "filename": "a.iso"})], claimable=False)
    monkeypatch.setattr(megarac_stream, "get_manager", lambda: fake)
    monkeypatch.setattr(megarac_stream, "RESUME_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(megarac_stream, "RESUME_RETRY_SECONDS", 0.01)
    _patch_server_lookup(monkeypatch, _server())
    await asyncio.wait_for(megarac_stream.resume_streams(), 5)
    assert fake.started == []


@pytest.mark.asyncio
async def test_resume_forgets_a_boot_iso_whose_task_has_finished(monkeypatch):
    fake = FakeResumeManager([(4, {"url": "rfboot://rfboot-a.iso", "filename": "rfboot-a.iso"})])
    fake.error = CdStreamError("Could not open the ISO: No pending boot for this image")
    monkeypatch.setattr(megarac_stream, "get_manager", lambda: fake)
    _patch_server_lookup(monkeypatch, _server())
    await megarac_stream.resume_streams()
    assert fake.forgotten == [4] and fake.released == [4]
