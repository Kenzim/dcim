"""Unit tests for services_client helper functions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from app.api.services_client import (
    _best_effort_power_state,
    _client_primary_ip,
    _client_service_availability,
    _client_service_detail_fields,
    _power_permission_key,
    _service_to_client_response,
    _validate_client_power_action,
)
from app.core.client_permissions import PermissionKey
from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.models.user import User
from app.plugins.base import PowerState


def _user(db, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _server(db, name: str, ip: str) -> Server:
    loc = Location(name=f"{name}-loc", description="")
    db.add(loc)
    db.flush()
    server = Server(
        name=name,
        server_ip=ip,
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db.add(server)
    db.commit()
    db.refresh(server)
    return server


def test_power_permission_key_by_type():
    vm = SimpleNamespace(service_type=ServiceType.VM)
    bm = SimpleNamespace(service_type=ServiceType.BARE_METAL)
    assert _power_permission_key(vm) == PermissionKey.VM_POWER
    assert _power_permission_key(bm) == PermissionKey.BMS_POWER


def test_client_primary_ip_vm_config_and_bare_metal(db_session):
    owner = _user(db_session, "ip-owner")
    server = _server(db_session, "ip-srv", "10.50.0.1")
    bm = ServiceDAO.create_bare_metal(
        db_session,
        name="bm-ip",
        server_id=server.id,
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    vm = ServiceDAO.create_vm(
        db_session,
        name="vm-ip",
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        config={"vm_ip_address": "203.0.113.10"},
    )
    assert _client_primary_ip(db_session, bm) == "10.50.0.1"
    assert _client_primary_ip(db_session, vm) == "203.0.113.10"


def test_client_service_availability_flags(db_session):
    owner = _user(db_session, "avail-owner")
    server = _server(db_session, "avail-srv", "10.60.0.1")
    server.ipmi_proxy_enabled = True
    server.ipmi_web_management_url = "https://bmc.local"
    db_session.commit()
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="avail-bm",
        server_id=server.id,
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    permissions = {
        PermissionKey.BMS_POWER: True,
        PermissionKey.BMS_IPMI: True,
        PermissionKey.BMS_KVM: False,
        PermissionKey.BMS_SOL: False,
        PermissionKey.BMS_VIRTUAL_MEDIA: False,
    }
    avail = _client_service_availability(
        service,
        server,
        permissions,
        cid=None,
        node=None,
        vmid=None,
    )
    assert avail["ipmi_available"] is True
    assert avail["power_available"] is True
    assert avail["kvm_console_available"] is False


def test_service_to_client_response_includes_ssh_fields(db_session):
    owner = _user(db_session, "resp-owner")
    service = ServiceDAO.create_vm(
        db_session,
        name="resp-vm",
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    with patch(
        "app.services.ssh_public_keys.ssh_key_fields_for_service",
        lambda db, svc: {
            "accepts_ssh_key": True,
            "has_ssh_public_keys": True,
            "ssh_public_keys_text": "ssh-ed25519 AAA",
            "needs_ssh_key_prompt": False,
        },
    ):
        resp = _service_to_client_response(service, db_session)
    assert resp.accepts_ssh_key is True
    assert resp.has_ssh_public_keys is True
    assert "ssh-ed25519" in resp.ssh_public_keys_text


def test_validate_client_power_action_guards(db_session):
    owner = _user(db_session, "pwr-guard")
    server = _server(db_session, "pwr-srv", "10.70.0.1")
    suspended = ServiceDAO.create_bare_metal(
        db_session,
        name="pwr-susp",
        server_id=server.id,
        owner_user_id=owner.id,
        status=ServiceStatus.SUSPENDED,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    with pytest.raises(HTTPException) as exc:
        _validate_client_power_action(suspended, "on", server)
    assert exc.value.status_code == 403

    disabled_server = _server(db_session, "pwr-off", "10.70.0.2")
    disabled_server.enabled = False
    db_session.commit()
    active = ServiceDAO.create_bare_metal(
        db_session,
        name="pwr-active",
        server_id=disabled_server.id,
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    with pytest.raises(HTTPException) as exc2:
        _validate_client_power_action(active, "reboot", disabled_server)
    assert exc2.value.status_code == 403


def test_client_service_detail_fields_masks_ipmi_when_unavailable(db_session):
    owner = _user(db_session, "detail-owner")
    server = _server(db_session, "detail-srv", "10.80.0.1")
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="detail-bm",
        server_id=server.id,
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    permissions = {PermissionKey.BMS_IPMI: False, PermissionKey.BMS_POWER: True}
    availability = _client_service_availability(
        service,
        server,
        permissions,
        cid=None,
        node=None,
        vmid=None,
    )
    fields = _client_service_detail_fields(
        db_session,
        service,
        server,
        permissions,
        PowerState.ON,
        availability,
    )
    assert fields["primary_ip"] == "10.80.0.1"
    assert fields["power_state"] == "on"
    assert fields["ipmi_viewer_username"] is None
    assert fields["installation"] is None


@pytest.mark.asyncio
async def test_best_effort_power_state_returns_unknown_on_failure(db_session):
    owner = _user(db_session, "pwr-unknown")
    service = ServiceDAO.create_vm(
        db_session,
        name="pwr-vm",
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    with patch(
        "app.api.services_client._client_plugin_instance",
        new=AsyncMock(side_effect=RuntimeError("no plugin")),
    ):
        state = await _best_effort_power_state(db_session, service)
    assert state == PowerState.UNKNOWN
