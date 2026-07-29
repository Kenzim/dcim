"""Tests for SSH public key parse/helpers, provision mapping, reinstall gate, manage API."""
from urllib.parse import unquote

import pytest

from app.dao.service_dao import ServiceDAO
from app.models.product_catalog import VMTemplate
from app.models.service import ProvisioningSource, ServiceStatus
from app.services.deployment.steps import ConfigureCloudInitNetworkStep, ConfigureViaGuestAgentStep
from app.services.ssh_public_keys import (
    SshPublicKeyError,
    format_authorized_keys,
    parse_ssh_public_keys,
    proxmox_sshkeys_param,
    service_needs_ssh_key_prompt,
    set_ssh_public_keys_on_service,
)
from tests.services.test_deployment_framework import FakeCtx, FakePlugin, _fake_alloc, _run


KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJustATestKeyAAAAAAAAAAAAAAA comment-a"
KEY_B = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7testKeyMaterialOnlyForUnitTestsXXXXXX comment-b"


def test_parse_multiline_and_reject_garbage():
    text = f"{KEY_A}\n\n# note\n{KEY_B}\n"
    keys = parse_ssh_public_keys(text)
    assert keys == [KEY_A, KEY_B]
    assert format_authorized_keys(keys) == f"{KEY_A}\n{KEY_B}"
    with pytest.raises(SshPublicKeyError):
        parse_ssh_public_keys("not-a-key\n")


def test_proxmox_sshkeys_encoding():
    encoded = proxmox_sshkeys_param([KEY_A, KEY_B])
    assert encoded
    assert unquote(encoded) == f"{KEY_A}\n{KEY_B}"


def test_cloudinit_payload_includes_all_sshkeys():
    plugin = FakePlugin()
    ctx = FakeCtx(
        plugin,
        alloc=_fake_alloc(),
        config={"template_parameters": {"ssh_public_keys": [KEY_A, KEY_B], "admin_password": "x"}},
    )
    _run(ConfigureCloudInitNetworkStep().execute(ctx))
    configure_calls = [c for c in plugin.calls if isinstance(c, tuple) and c[0] == "configure"]
    assert configure_calls
    payload = configure_calls[0][1]
    assert "sshkeys" in payload
    assert unquote(payload["sshkeys"]) == f"{KEY_A}\n{KEY_B}"


def test_cloudinit_skips_sshkeys_when_empty():
    plugin = FakePlugin()
    ctx = FakeCtx(plugin, alloc=_fake_alloc(), config={"template_parameters": {"admin_password": "x"}})
    _run(ConfigureCloudInitNetworkStep().execute(ctx))
    payload = [c for c in plugin.calls if isinstance(c, tuple) and c[0] == "configure"][0][1]
    assert "sshkeys" not in payload


def test_guest_agent_writes_authorized_keys_full_replace():
    plugin = FakePlugin(agent=True)
    ctx = FakeCtx(
        plugin,
        alloc=_fake_alloc(),
        config={
            "template_parameters": {"ssh_public_keys": [KEY_A], "admin_password": "secret"},
            "vm_plan": {"strategy_plan": {"strategy_config": {"guest_username": "root", "network_mode": "dhcp"}}},
        },
    )
    _run(ConfigureViaGuestAgentStep().execute(ctx))
    exec_calls = [c for c in plugin.calls if isinstance(c, tuple) and c[0] == "guest_exec"]
    assert exec_calls
    script = exec_calls[-1][1][2] if len(exec_calls[-1][1]) > 2 else ""
    # Full replace path writes authorized_keys via base64 decode
    assert "authorized_keys" in script
    assert "base64 -d" in script


def test_service_needs_ssh_key_prompt(db_session):
    tmpl = VMTemplate(
        code="debian-13",
        name="linux-ci",
        os_type="Linux - Cloudinit",
        proxmox_template_name="debian-13",
        enabled=True,
    )
    db_session.add(tmpl)
    db_session.commit()
    db_session.refresh(tmpl)

    service = ServiceDAO.create_vm(
        db_session,
        name="ssh-prompt",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        vm_template_id=tmpl.id,
    )
    service.config = {
        "vm_plan": {
            "strategy_name": "cloudinit_clone",
            "vm_template": {"id": tmpl.id, "os_type": tmpl.os_type},
        }
    }
    ServiceDAO.update(db_session, service)
    assert service_needs_ssh_key_prompt(db_session, service) is True
    set_ssh_public_keys_on_service(service, [KEY_A])
    ServiceDAO.update(db_session, service)
    assert service_needs_ssh_key_prompt(db_session, service) is False


def test_manage_keys_api_updates_and_applies(client, db_session, test_admin_user, monkeypatch):
    tmpl = VMTemplate(
        code="debian-13-b",
        name="linux-ci-2",
        os_type="Linux - Cloudinit",
        proxmox_template_name="debian-13",
        enabled=True,
    )
    db_session.add(tmpl)
    db_session.commit()
    db_session.refresh(tmpl)

    service = ServiceDAO.create_vm(
        db_session,
        name="ssh-manage",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        vm_template_id=tmpl.id,
        proxmox_cluster_id=1,
        proxmox_node_name="pve1",
    )
    service.vm.proxmox_vmid = 4242
    service.config = {"vm_plan": {"strategy_name": "cloudinit_clone"}}
    ServiceDAO.update(db_session, service)

    applied = {"called": False}

    async def _fake_apply(db, service, keys):
        applied["called"] = True
        applied["keys"] = list(keys)
        return True

    monkeypatch.setattr(
        "app.services.vm_ssh_keys_service._try_apply_authorized_keys",
        _fake_apply,
    )

    login = client.post(
        "/api/users/login",
        json={"username": "admin", "password": "adminpassword123"},
    )
    assert login.status_code == 200
    token = login.json()["token"]

    resp = client.put(
        f"/api/admin/services/{service.id}/vm/ssh-keys",
        headers={"Authorization": f"Bearer {token}"},
        json={"ssh_public_keys": f"{KEY_A}\n{KEY_B}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["has_ssh_public_keys"] is True
    assert KEY_A in body["ssh_public_keys"]
    assert applied["called"] is True

    get_resp = client.get(
        f"/api/admin/services/{service.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert get_resp.status_code == 200
    svc = get_resp.json()
    assert svc["accepts_ssh_key"] is True
    assert svc["has_ssh_public_keys"] is True
    assert KEY_A in svc["ssh_public_keys_text"]
