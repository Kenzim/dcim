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
# DHCP interface IPs (next-server) of the two subnets in the fake DHCP config.
NEXT_SERVER_A = "203.0.113.2"
NEXT_SERVER_B = "198.51.100.2"
MAC_B = "00:0e:1e:6f:16:b1"
PXE_IP_B = "198.51.100.20"


class _Profile(VirtualMediaProfile):
    id = PROFILE_ID
    display_name = "Test vmedia"

    def __init__(self, fail_insert=False):
        self.events = []
        self.fail_insert = fail_insert
        self.report_inserted = True

    async def status(self, server):
        return VirtualMediaStatus(inserted=False)

    async def insert(self, server, image_url):
        self.events.append(("insert", image_url))
        if self.fail_insert:
            from app.services.virtual_media.base import VirtualMediaUnavailable

            raise VirtualMediaUnavailable("BMC rejected the image")
        return VirtualMediaStatus(inserted=self.report_inserted, image_name=image_url)

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


def _vmedia_server(db, location_id, name, ip, mac):
    server = ServerDAO.create(
        db,
        name=name,
        server_ip=ip,
        plugin_name="ipmi",
        plugin_config={"hostname": "10.0.0.5", "username": "admin", "password": "secret"},
        location_id=location_id,
        virtual_media_profile=PROFILE_ID,
    )
    NetworkPortDAO.create(
        db, server_id=server.id, name="eth0", mac_address=mac,
        speed_mbps=1000, pxe_boot=True, pxe_ip=ip,
    )
    return server


@pytest.fixture
def vmedia_env(client, db_session, monkeypatch, tmp_path):
    """Server with a vmedia profile, a two-subnet DHCP config and a fake ISO template."""
    from app.services.dhcp_config_service import DHCPConfig, DHCPInterfaceConfig

    placeholder = boot_iso.PLACEHOLDER.read_bytes()
    template = tmp_path / "rackflow-vmedia.iso"
    template.write_bytes(b"ISO" + placeholder + b"--" + placeholder + b"END")
    monkeypatch.setattr(boot_iso, "TEMPLATE_ISO", template)
    boot_iso._template_cache.clear()
    monkeypatch.setattr(settings, "public_base_url", "https://rackflow.example.com")
    monkeypatch.setattr(settings, "virtual_media_base_url", "http://bmc-fetch.test")
    monkeypatch.setattr(settings, "vmedia_boot_base_url", None)
    dhcp = DHCPConfig(
        enabled=True,
        interfaces=[
            DHCPInterfaceConfig(interface="eth0", ip=NEXT_SERVER_A, cidr=24),
            DHCPInterfaceConfig(interface="eth1", ip=NEXT_SERVER_B, cidr=24),
        ],
        config_file_path="/dev/null",
        lease_file_path="/dev/null",
    )
    monkeypatch.setattr(
        "app.services.dhcp_config_service.get_dhcp_config_service",
        lambda: SimpleNamespace(get_config=lambda _db: dhcp, get_config_by_service_instance=lambda _db, _i: dhcp),
    )

    profile = _Profile()
    register_profile(profile)
    location = LocationDAO.create(db_session, name="loc-vmedia")
    server = _vmedia_server(db_session, location.id, "vmedia-srv", PXE_IP, MAC)
    plugin = _plugin(profile.events)
    monkeypatch.setattr(
        "app.plugins.registry.get_registry",
        lambda: SimpleNamespace(get_plugin=lambda *_a, **_k: plugin),
    )
    yield SimpleNamespace(server=server, profile=profile, plugin=plugin, db=db_session, location=location)
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


def test_launch_failure_ejects_and_falls_back_to_pxe(vmedia_env, _no_background_dhcp_sync):
    env = vmedia_env
    env.profile.fail_insert = True
    task = _task(env.db, env.server.id)
    _no_background_dhcp_sync.clear()
    method = asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test"))
    assert method == "pxe"
    env.db.refresh(task)
    assert task.status == BootTaskStatus.PENDING
    assert task.boot_method == "pxe"
    assert task.pxe_nonce_used_at is None
    assert ("eject",) in env.profile.events
    assert mounted_filename(env.server.id) is None
    assert not any(e[0] == "bootdev" or e[0].startswith("power") for e in env.profile.events)
    assert env.server.id in _no_background_dhcp_sync  # PXE options re-advertised


def test_launch_does_not_power_cycle_into_empty_cd(vmedia_env, _no_background_dhcp_sync):
    env = vmedia_env
    env.profile.report_inserted = False
    task = _task(env.db, env.server.id)
    method = asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test"))
    assert method == "pxe"
    env.db.refresh(task)
    assert task.status == BootTaskStatus.PENDING
    assert task.boot_method == "pxe"
    assert ("eject",) in env.profile.events
    assert not any(e[0] == "bootdev" or e[0].startswith("power") for e in env.profile.events)


def test_launch_failure_is_logged_with_bmc_detail(vmedia_env, monkeypatch):
    env = vmedia_env
    env.profile.fail_insert = True
    logged = []
    monkeypatch.setattr(boot_launcher, "log_server_activity_failure", lambda db, **kw: logged.append(kw))
    asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, _task(env.db, env.server.id), source="test"))
    assert logged and logged[0]["action"] == "vmedia_boot"
    assert logged[0]["details"]["fallback"] == "pxe"
    assert "BMC rejected" in logged[0]["details"]["detail"]


def test_launch_without_vmedia_profile_stays_pxe(vmedia_env):
    env = vmedia_env
    env.server.virtual_media_profile = None
    env.db.commit()
    task = _task(env.db, env.server.id)
    assert asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test")) == "pxe"
    assert task.boot_method == "pxe"
    assert env.profile.events == []


def test_launch_with_invalid_boot_url_override_stays_pxe(vmedia_env, monkeypatch):
    env = vmedia_env
    monkeypatch.setattr(settings, "vmedia_boot_base_url", "10.0.0.1:8000")
    task = _task(env.db, env.server.id)
    assert asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test")) == "pxe"


def test_launch_needs_no_public_or_boot_url(vmedia_env, monkeypatch):
    env = vmedia_env
    monkeypatch.setattr(settings, "public_base_url", None)
    monkeypatch.setattr(settings, "public_app_url", None)
    task = _task(env.db, env.server.id)
    assert asyncio.run(boot_launcher.launch_boot_task(env.db, env.server, task, source="test")) == "vmedia"


@pytest.mark.parametrize("nfs_export_in_state", [True, False])
def test_runner_stage_uses_nfs_for_nfs_profiles(vmedia_env, monkeypatch, nfs_export_in_state):
    from app.services.virtual_media.registry import get_profile

    env = vmedia_env
    rpc_calls = []

    async def rpc(runner_id, method, params, timeout=None):
        rpc_calls.append((method, params))
        return {"job_id": "job1"} if method == "media.download" else {}

    state = {"smb_host": "10.0.0.9", "jobs": [{"id": "job1", "status": "done"}]}
    if nfs_export_in_state:
        state["nfs_export"] = "/srv/isos"
    else:
        monkeypatch.setattr(settings, "virtual_media_nfs_export", "/srv/isos")
    monkeypatch.setattr(boot_launcher, "_media_runner", lambda db, server: SimpleNamespace(id=7, state={}))
    monkeypatch.setattr("app.services.runners.hub.get_hub", lambda: SimpleNamespace(rpc=rpc))
    monkeypatch.setattr("app.services.runners.cache.cached_state", lambda runner_id: state)
    url = asyncio.run(
        boot_launcher._stage_boot_iso(env.db, env.server, get_profile("asrockrack"), "rfboot-abc.iso")
    )
    assert url == "nfs://10.0.0.9/srv/isos/rfboot-abc.iso"
    assert [m for m, _ in rpc_calls] == ["media.download"]


def test_runner_stage_nfs_without_export_raises(vmedia_env, monkeypatch):
    from app.services.virtual_media.registry import get_profile

    async def rpc(runner_id, method, params, timeout=None):
        return {"job_id": "job1"}

    monkeypatch.setattr(settings, "virtual_media_nfs_export", "")
    monkeypatch.setattr(boot_launcher, "_media_runner", lambda db, server: SimpleNamespace(id=7, state={}))
    monkeypatch.setattr("app.services.runners.hub.get_hub", lambda: SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(
        "app.services.runners.cache.cached_state",
        lambda runner_id: {"smb_host": "10.0.0.9", "jobs": [{"id": "job1", "status": "done"}]},
    )
    with pytest.raises(boot_launcher.BootLaunchError, match="no NFS export"):
        asyncio.run(
            boot_launcher._stage_boot_iso(vmedia_env.db, vmedia_env.server, get_profile("asrockrack"), "rfboot-a.iso")
        )


@pytest.mark.parametrize("profile_id", ["supermicro_x9"])
def test_runner_stage_uses_smb_for_cifs_profiles(vmedia_env, monkeypatch, profile_id):
    from app.services.virtual_media.registry import get_profile

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
    profile = get_profile(profile_id)
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


def test_runner_stage_refuses_public_url_when_location_runner_disconnected(vmedia_env, monkeypatch):
    env = vmedia_env
    monkeypatch.setattr(boot_launcher, "_media_runner", lambda db, server: None)
    monkeypatch.setattr(boot_launcher, "_location_has_media_runner", lambda db, server: True)
    with pytest.raises(boot_launcher.BootLaunchError, match="not connected"):
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
    assert full.content.count(f"chain http://{NEXT_SERVER_A}:8000/api/servers/interaction/pxe".encode()) == 2
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


def test_pxe_for_vmedia_task_uses_subnet_next_server_and_releases_iso(pxe_get, vmedia_env, boot_launcher_calls):
    env = vmedia_env
    task = _task(env.db, env.server.id)
    task.boot_method = "vmedia"
    env.db.commit()
    response = pxe_get(MAC, task, source_ip=PXE_IP)
    assert response.status_code == 200, response.text
    assert f"kernel http://{NEXT_SERVER_A}:8000/" in response.text
    assert "rackflow.example.com" not in response.text
    assert boot_launcher_calls["release"] == [env.server.id]


def test_vmedia_boot_origin_follows_each_servers_subnet(client, pxe_get, vmedia_env, boot_launcher_calls):
    """Two DHCP subnets in one location: each server chains to its own next-server."""
    env = vmedia_env
    server_b = _vmedia_server(env.db, env.location.id, "vmedia-srv-b", PXE_IP_B, MAC_B)
    task = _task(env.db, server_b.id)
    task.boot_method = "vmedia"
    env.db.commit()

    name = "rfboot-0123456789abcdef0123456789abcdef.iso"
    iso = client.get(f"/api/virtual-media/images/{mint_image_token(server_b.id, name)}/{name}")
    assert iso.status_code == 200, iso.text
    assert iso.content.count(f"chain http://{NEXT_SERVER_B}:8000/api/servers/interaction/pxe".encode()) == 2
    assert NEXT_SERVER_A.encode() not in iso.content

    response = pxe_get(MAC_B, task, source_ip=PXE_IP_B)
    assert response.status_code == 200, response.text
    assert f"kernel http://{NEXT_SERVER_B}:8000/" in response.text
    assert NEXT_SERVER_A not in response.text


def test_pxe_for_vmedia_task_boot_url_override_wins(pxe_get, vmedia_env, boot_launcher_calls, monkeypatch):
    env = vmedia_env
    monkeypatch.setattr(settings, "vmedia_boot_base_url", "http://10.0.0.1:8000")
    task = _task(env.db, env.server.id)
    task.boot_method = "vmedia"
    env.db.commit()
    response = pxe_get(MAC, task, source_ip=PXE_IP)
    assert response.status_code == 200, response.text
    assert "kernel http://10.0.0.1:8000/" in response.text
    assert NEXT_SERVER_A not in response.text


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
