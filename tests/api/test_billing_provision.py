"""Direct unit coverage for local billing provisioning helpers."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.billing as billing
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.dao.user_dao import UserDAO
from app.models.service import ServiceType
from app.schemas.billing import BillingBareMetalServiceCreate, BillingVmServiceCreate
from app.services.provisioning import ProvisioningActor


def _actor():
    return ProvisioningActor(kind="integration", actor_id=1, name="test", source="test")


@pytest.mark.asyncio
async def test_provision_bare_metal_creates_server_and_service(db_session, monkeypatch):
    owner = UserDAO.create(db_session, username="provision-owner", email="provision@example.test")
    location = LocationDAO.create(db_session, name="provision-location")
    server = ServerDAO.create(
        db_session,
        name="pooled-srv",
        server_ip="203.0.113.20",
        plugin_name="ipmi",
        plugin_config={},
        location_id=location.id,
    )
    group = ServerGroupDAO.create(
        db_session,
        name="provision-pool",
        enable_os_templates=True,
        permitted_os_templates=["ubuntu-cloud-image"],
    )
    group.servers.append(server)
    db_session.commit()
    monkeypatch.setattr(
        "app.api.billing._queue_template_install_for_service",
        lambda **kwargs: (SimpleNamespace(id=1), SimpleNamespace(id=2, boot_task_id=1)),
    )
    data = BillingBareMetalServiceCreate(
        name="provisioned-bm",
        external_user_id="external",
        service_config={"server_group_id": group.id, "template_id": "ubuntu-cloud-image"},
    )
    service = billing._provision_bare_metal_service(data, owner.id, _actor(), db_session)
    assert service.service_type == ServiceType.BARE_METAL
    assert service.bare_metal.server.server_ip == "203.0.113.20"


@pytest.mark.asyncio
async def test_provision_bare_metal_rejects_invalid_inputs(db_session):
    owner = UserDAO.create(db_session, username="provision-errors", email="errors@example.test")
    base = {"name": "bad-provision", "external_user_id": "external"}
    invalid_type = BillingBareMetalServiceCreate(**base, service_type="invalid")
    with pytest.raises(HTTPException) as exc:
        billing._provision_bare_metal_service(invalid_type, owner.id, _actor(), db_session)
    assert exc.value.status_code == 400
    vm_type = BillingBareMetalServiceCreate(name="bad-vm", external_user_id="external", service_type="vm")
    with pytest.raises(HTTPException) as exc:
        billing._provision_bare_metal_service(vm_type, owner.id, _actor(), db_session)
    assert exc.value.status_code == 400

    missing_group = BillingBareMetalServiceCreate(name="no-group", external_user_id="external")
    with pytest.raises(HTTPException) as exc:
        billing._provision_bare_metal_service(missing_group, owner.id, _actor(), db_session)
    assert exc.value.status_code == 400
    assert "server_group_id" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_provision_vm_validation_errors(db_session):
    owner = UserDAO.create(db_session, username="vm-errors", email="vm-errors@example.test")
    for body in (
        BillingVmServiceCreate(name="vm-group", external_user_id="e", service_config={"server_group_id": 1}),
        BillingVmServiceCreate(name="vm-template", external_user_id="e", vm_template_id=1),
    ):
        with pytest.raises(HTTPException) as exc:
            billing._provision_vm_service(body, owner.id, _actor(), db_session)
        assert exc.value.status_code == 400
