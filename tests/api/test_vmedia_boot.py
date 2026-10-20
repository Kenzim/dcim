"""Virtual-media (ISO) network boot: launcher, stamped image route, /pxe, install hooks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.dao import BootTaskDAO, NetworkPortDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.models.boot_task import BootTaskStatus
from app.plugins.base import PowerState
from app.services import boot_launcher
from app.services.virtual_media import boot_iso
from app.services.virtual_media.base import VirtualMediaProfile, VirtualMediaStatus
from app.services.virtual_media.image_token import mint_image_token, mounted_filename
from app.services.virtual_media.registry import register_profile, unregister_profile

PROFILE_ID = "test_vmedia_boot"
MAC = "00:0e:1e:6f:16:b0"
PXE_IP = "203.0.113.10"


class _Profile(VirtualMediaProfile):
    id = PROFILE_ID
    display_name = "Test vmedia"

    def __init__(self, fail_insert=False):
        self.events = []
        self.fail_insert = fail_insert

    async def status(self, server):
        return VirtualMediaStatus(inserted=False)

    async def insert(self, server, image_url):
        self.events.append(("insert", image_url))
        if self.fail_insert:
            from app.services.virtual_media.base import VirtualMediaUnavailable

            raise VirtualMediaUnavailable("BMC rejected the image")
        return VirtualMediaStatus(inserted=True, image_name=image_url)

    async def eject(self, server):
        self.events.append(("eject",))
        return VirtualMediaStatus(inserted=False)


def _plugin(events, power=PowerState.ON, boot_ok=True):
    async def set_next_boot_device(device, persistent=False, uefi=None):
        events.append(("bootdev", device, persistent, uefi))
        return boot_ok

    async def power_reset():
        events.append(("power_reset",))
        return True

    async def power_on():
        events.append(("power_on",))
        return True

    return SimpleNamespace(
        set_next_boot_device=set_next_boot_device,
        power_reset=power_reset,
        power_on=power_on,
        get_power_state=AsyncMock(return_value=power),
    )


@pytest.fixture
def vmedia_env(client, db_session, monkeypatch, tmp_path):
    """Server with a vmedia profile, an https public URL and a fake ISO template."""
    placeholder = boot_iso.PLACEHOLDER.read_bytes()
    template = tmp_path / "rackflow-vmedia.iso"
    template.write_bytes(b"ISO" + placeholder + b"--" + placeholder + b"END")
    monkeypatch.setattr(boot_iso, "TEMPLATE_ISO", template)
    boot_iso._template_cache.clear()
    monkeypatch.setattr(settings, "public_base_url", "https://rackflow.example.com")
    monkeypatch.setattr(settings, "virtual_media_base_url", "http://bmc-fetch.test")
    monkeypatch.setattr(settings, "vmedia_boot_base_url", None)

    profile = _Profile()
    register_profile(profile)
    location = LocationDAO.create(db_session, name="loc-vmedia")
    server = ServerDAO.create(
        db_session,
        name="vmedia-srv",
        server_ip=PXE_IP,
        plugin_name="ipmi",
        plugin_config={"hostname": "10.0.0.5", "username": "admin", "password": "secret"},
        location_id=location.id,
        virtual_media_profile=PROFILE_ID,
    )
    NetworkPortDAO.create(
        db_session, server_id=server.id, name="eth0", mac_address=MAC,
        speed_mbps=1000, pxe_boot=True, pxe_ip=PXE_IP,
    )
    plugin = _plugin(profile.events)
    monkeypatch.setattr(
        "app.plugins.registry.get_registry",
        lambda: SimpleNamespace(get_plugin=lambda *_a, **_k: plugin),
    )
    yield SimpleNamespace(server=server, profile=profile, plugin=plugin, db=db_session)
    unregister_profile(PROFILE_ID)


def _task(db, server_id, boot_type="linux_script"):
    return BootTaskDAO.create(db=db, server_id=server_id, boot_type=boot_type, script_content="#!/bin/sh\necho hi\n")


def test_launch_mounts_iso_boot_once_cdrom_then_power_cycles(vmedia_env, _no_background_dhcp_sync):
    env = vmedia_env
    task = _task(env.db, env.server.id)
    method = asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test"))
    assert method == "vmedia"
    assert task.boot_method == "vmedia"
    kinds = [e[0] for e in env.profile.events]
    assert kinds == ["insert", "bootdev", "power_reset"]
    _, url = env.profile.events[0]
    assert url.startswith("http://bmc-fetch.test/api/virtual-media/images/")
    assert boot_iso.is_boot_iso_name(url.rsplit("/", 1)[1])
    assert env.profile.events[1] == ("bootdev", "cdrom", False, True)  # one-time, UEFI
    assert boot_iso.is_boot_iso_name(mounted_filename(env.server.id))
    assert env.server.id in _no_background_dhcp_sync  # PXE options dropped for this host


def test_launch_powers_on_when_off(vmedia_env):
    env = vmedia_env
    env.plugin.get_power_state.return_value = PowerState.OFF
    asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, _task(env.db, env.server.id), source="test"))
    assert [e[0] for e in env.profile.events][-1] == "power_on"


def test_launch_failure_ejects_and_fails_task(vmedia_env):
    env = vmedia_env
    env.profile.fail_insert = True
    task = _task(env.db, env.server.id)
    with pytest.raises(boot_launcher.BootLaunchError, match="BMC rejected"):
        asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test"))
    env.db.refresh(task)
    assert task.status == BootTaskStatus.FAILED
    assert "BMC rejected" in task.error_message
    assert ("eject",) in env.profile.events
    assert mounted_filename(env.server.id) is None
    assert not any(e[0].startswith("power") for e in env.profile.events)


def test_launch_without_vmedia_profile_stays_pxe(vmedia_env):
    env = vmedia_env
    env.server.virtual_media_profile = None
    env.db.commit()
    task = _task(env.db, env.server.id)
    assert asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test")) == "pxe"
    assert task.boot_method == "pxe"
    assert env.profile.events == []


def test_launch_without_https_public_url_stays_pxe(vmedia_env, monkeypatch):
    env = vmedia_env
    monkeypatch.setattr(settings, "public_base_url", "http://rackflow.example.com")
    monkeypatch.setattr(settings, "public_app_url", None)
    task = _task(env.db, env.server.id)
    assert asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test")) == "pxe"


def test_launch_with_lan_http_boot_url_uses_vmedia(vmedia_env, monkeypatch):
    env = vmedia_env
    monkeypatch.setattr(settings, "public_base_url", "http://rackflow.example.com")
    monkeypatch.setattr(settings, "public_app_url", None)
    monkeypatch.setattr(settings, "vmedia_boot_base_url", "http://10.0.0.1:8000")
    task = _task(env.db, env.server.id)
    assert asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test")) == "vmedia"


def test_runner_stage_uses_smb_for_supermicro_x9(vmedia_env, monkeypatch):
    env = vmedia_env
    rpc_calls = []

    async def rpc(runner_id, method, params, timeout=None):
        rpc_calls.append((method, params))
        if method == "media.download":
            return {"job_id": "job1"}
        if method == "media.mint_smb_credentials":
            return {"host": "10.0.0.9", "share": "isos", "user": "vmu", "password": "pw"}
        return {}

    monkeypatch.setattr(boot_launcher, "_media_runner", lambda db, server: SimpleNamespace(id=7, state={}))
    monkeypatch.setattr("app.services.runners.hub.get_hub", lambda: SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(
        "app.services.runners.cache.cached_state",
        lambda runner_id: {"jobs": [{"id": "job1", "status": "done"}]},
    )
    profile = SimpleNamespace(id="supermicro_x9")
    url = asyncio.run(boot_launcher._stage_boot_iso(env.db, env.server, profile, "rfboot-abc.iso"))
    assert url == "cifs://vmu:pw@10.0.0.9/isos/rfboot-abc.iso"
    method, params = rpc_calls[0]
    assert method == "media.download"
    assert params["filename"] == "rfboot-abc.iso"
    assert params["url"].startswith("http://bmc-fetch.test/api/virtual-media/images/")


def test_runner_stage_job_error_raises(vmedia_env, monkeypatch):
    env = vmedia_env

    async def rpc(runner_id, method, params, timeout=None):
        return {"job_id": "job1"}

    monkeypatch.setattr(boot_launcher, "_media_runner", lambda db, server: SimpleNamespace(id=7, state={}))
    monkeypatch.setattr("app.services.runners.hub.get_hub", lambda: SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(
        "app.services.runners.cache.cached_state",
        lambda runner_id: {"jobs": [{"id": "job1", "status": "error", "error": "404"}]},
    )
    with pytest.raises(boot_launcher.BootLaunchError, match="404"):
        asyncio.run(boot_launcher._stage_boot_iso(env.db, env.server, SimpleNamespace(id="x"), "rfboot-a.iso"))


def test_image_route_serves_stamped_iso_with_range(client, vmedia_env):
    env = vmedia_env
    task = _task(env.db, env.server.id)
    task.boot_method = "vmedia"
    env.db.commit()
    name = "rfboot-0123456789abcdef0123456789abcdef.iso"
    token = mint_image_token(env.server.id, name)

    full = client.get(f"/api/virtual-media/images/{token}/{name}")
    assert full.status_code == 200, full.text
    assert full.content.count(f"&t={task.pxe_nonce}".encode()) == 2
    assert boot_iso.PLACEHOLDER.read_bytes() not in full.content

    part = client.get(f"/api/virtual-media/images/{token}/{name}", headers={"Range": "bytes=0-2"})
    assert part.status_code == 206
    assert part.content == b"ISO"


def test_image_route_404_once_nonce_used(client, vmedia_env):
    env = vmedia_env
    task = _task(env.db, env.server.id)
    task.boot_method = "vmedia"
    env.db.commit()
    BootTaskDAO.consume_pxe_nonce(env.db, task.id)
    name = "rfboot-0123456789abcdef0123456789abcdef.iso"
    token = mint_image_token(env.server.id, name)
    assert client.get(f"/api/virtual-media/images/{token}/{name}").status_code == 404


def test_pxe_for_vmedia_task_uses_https_and_releases_iso(pxe_get, vmedia_env, boot_launcher_calls):
    env = vmedia_env
    task = _task(env.db, env.server.id)
    task.boot_method = "vmedia"
    env.db.commit()
    response = pxe_get(MAC, task, source_ip=PXE_IP)
    assert response.status_code == 200, response.text
    assert "kernel https://rackflow.example.com/" in response.text
    assert "http://" not in response.text
    assert boot_launcher_calls["release"] == [env.server.id]


def test_pxe_for_vmedia_task_uses_lan_http_boot_url(pxe_get, vmedia_env, boot_launcher_calls, monkeypatch):
    env = vmedia_env
    monkeypatch.setattr(settings, "vmedia_boot_base_url", "http://10.0.0.1:8000")
    task = _task(env.db, env.server.id)
    task.boot_method = "vmedia"
    env.db.commit()
    response = pxe_get(MAC, task, source_ip=PXE_IP)
    assert response.status_code == 200, response.text
    assert "kernel http://10.0.0.1:8000/" in response.text
    assert "rackflow.example.com" not in response.text


@pytest.mark.parametrize("method", ["vmedia", "pxe"])
def test_pxe_temp_os_flags_vmedia_boot_on_cmdline(pxe_get, vmedia_env, boot_launcher_calls, method):
    """Install scripts (e.g. Windows EFI entry handling) read rf_boot_method from /proc/cmdline."""
    env = vmedia_env
    task = BootTaskDAO.create(
        db=env.db, server_id=env.server.id, boot_type="temp_os", temp_os_id="debian-live",
        script_content="#!/bin/bash\necho install\n",
    )
    task.boot_method = method
    env.db.commit()
    response = pxe_get(MAC, task, source_ip=PXE_IP)
    assert response.status_code == 200, response.text
    assert ("rf_boot_method=vmedia" in response.text) == (method == "vmedia")


def test_dhcp_reservation_only_for_vmedia_task(vmedia_env):
    from unittest.mock import MagicMock, patch

    from app.services.dhcp_config_generator import generate_dhcpd_conf
    from app.services.dhcp_config_service import DHCPConfig, DHCPInterfaceConfig

    env = vmedia_env
    task = _task(env.db, env.server.id)
    task.boot_method = "vmedia"
    env.db.commit()
    cfg = DHCPConfig(
        enabled=True,
        interfaces=[DHCPInterfaceConfig(interface="eth0", ip="203.0.113.2", cidr=24)],
        config_file_path="/dev/null",
        lease_file_path="/dev/null",
    )
    with patch("app.services.dhcp_config_generator.ServerDAO.get_all", return_value=[env.server]):
        content, _ = generate_dhcpd_conf(cfg, env.db, return_content=True)
    assert f"fixed-address {PXE_IP};" in content
    assert "filename" not in content
    assert task.pxe_nonce not in content


def test_admin_boot_task_schedules_launch(client, test_admin_user, vmedia_env, boot_launcher_calls):
    env = vmedia_env
    login = client.post("/api/users/login", json={"username": test_admin_user.username, "password": "adminpassword123"})
    assert login.status_code == 200
    response = client.post(
        f"/api/servers/interaction/{env.server.id}/boot-task",
        json={"boot_type": "linux_script", "script_content": "#!/bin/sh\necho hi\n"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["boot_method"] == "vmedia"
    assert boot_launcher_calls["launch"] == [(env.server.id, body["id"], "admin_api", None)]

    no_launch = client.post(
        f"/api/servers/interaction/{env.server.id}/boot-task",
        json={"boot_type": "linux_script", "script_content": "#!/bin/sh\n", "launch": False},
    )
    assert no_launch.json()["boot_method"] == "pxe"
    assert len(boot_launcher_calls["launch"]) == 1


def test_boot_order_fix_skipped_for_vmedia_installs(vmedia_env, monkeypatch):
    from app.api import installation_tasks

    env = vmedia_env
    vmedia_task = _task(env.db, env.server.id)
    vmedia_task.boot_method = "vmedia"
    env.db.commit()
    pxe_task = _task(env.db, env.server.id)

    created = []
    monkeypatch.setattr(installation_tasks.BootTaskDAO, "create", lambda *a, **k: created.append(k))
    template = SimpleNamespace(post_install_fix_boot_order=True, efi_boot_loader=None)
    monkeypatch.setattr(
        installation_tasks, "get_template_service",
        lambda: SimpleNamespace(get_template=lambda _id: template),
    )

    install = SimpleNamespace(template_id="windows-server-2022", server_id=env.server.id, boot_task_id=vmedia_task.id)
    installation_tasks._schedule_post_install_boot_order_fix(env.db, install)
    assert created == []

    install.boot_task_id = pxe_task.id
    installation_tasks._schedule_post_install_boot_order_fix(env.db, install)
    assert len(created) == 1
