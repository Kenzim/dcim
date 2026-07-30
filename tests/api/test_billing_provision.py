"""Direct unit coverage for local billing provisioning helpers."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.billing as billing
from app.dao.location_dao import LocationDAO
from app.dao.user_dao import UserDAO
from app.models.service import ServiceType
from app.schemas.billing import BillingBareMetalServiceCreate, BillingVmServiceCreate
from app.services.billing_provisioning_service import ProvisioningActor


def _actor():
    return ProvisioningActor(kind="integration", actor_id=1, name="test", source="test")


@pytest.mark.asyncio
async def test_provision_bare_metal_creates_server_and_service(db_session, monkeypatch):
    owner = UserDAO.create(db_session, username="provision-owner", email="provision@example.test")
    location = LocationDAO.create(db_session, name="provision-location")
    registry = SimpleNamespace(get_plugin_class=lambda name: object())
    monkeypatch.setattr(billing, "get_registry", lambda: registry)
    data = BillingBareMetalServiceCreate(
        name="provisioned-bm", external_user_id="external", location_id=location.id,
        plugin_name="ipmi", server_ip="203.0.113.20", os_boot_mode="bios",
        disks=[{"type": "ssd", "capacity_gb": 100, "is_os_disk": True}],
        network_ports=[{"name": "eth0", "mac_address": "00:11:22:33:44:55", "pxe_boot": True}],
    )
    service = await billing._provision_bare_metal_service(data, owner.id, _actor(), db_session)
    assert service.service_type == ServiceType.BARE_METAL
    assert service.bare_metal.server.server_ip == "203.0.113.20"
    assert service.bare_metal.server.os_boot_mode.value == "bios"


@pytest.mark.asyncio
async def test_provision_bare_metal_rejects_invalid_inputs(db_session, monkeypatch):
    owner = UserDAO.create(db_session, username="provision-errors", email="errors@example.test")
    base = {"name": "bad-provision", "external_user_id": "external"}
    invalid_type = BillingBareMetalServiceCreate(**base, service_type="invalid")
    with pytest.raises(HTTPException) as exc:
        await billing._provision_bare_metal_service(invalid_type, owner.id, _actor(), db_session)
    assert exc.value.status_code == 400
    vm_type = BillingBareMetalServiceCreate(name="bad-vm", external_user_id="external", service_type="vm")
    with pytest.raises(HTTPException) as exc:
        await billing._provision_bare_metal_service(vm_type, owner.id, _actor(), db_session)
    assert exc.value.status_code == 400

    registry = SimpleNamespace(get_plugin_class=lambda name: None)
    monkeypatch.setattr(billing, "get_registry", lambda: registry)
    no_plugin = BillingBareMetalServiceCreate(
        name="no-plugin", external_user_id="external", plugin_name="missing", location_id=1,
    )
    with pytest.raises(HTTPException) as exc:
        await billing._provision_bare_metal_service(no_plugin, owner.id, _actor(), db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_provision_vm_validation_errors(db_session):
    owner = UserDAO.create(db_session, username="vm-errors", email="vm-errors@example.test")
    for body in (
        BillingVmServiceCreate(name="vm-group", external_user_id="e", service_config={"server_group_id": 1}),
        BillingVmServiceCreate(name="vm-template", external_user_id="e", vm_template_id=1),
    ):
        with pytest.raises(HTTPException) as exc:
            await billing._provision_vm_service(body, owner.id, _actor(), SimpleNamespace(), db_session)
        assert exc.value.status_code == 400
