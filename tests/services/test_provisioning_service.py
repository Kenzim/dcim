"""Unified ProvisioningService: one create path per service type."""
from types import SimpleNamespace

import pytest

from app.dao.location_dao import LocationDAO
from app.dao.product_catalog_dao import ProductDAO, ProductFamilyDAO, VMTemplateDAO
from app.dao.server_dao import ServerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.services.provisioning import (
    ProvisionRequest,
    ProvisioningActor,
    ProvisioningError,
    ProvisioningService,
)


def _actor():
    return ProvisioningActor(kind="test", actor_id=1, name="pytest", source="test")


def _owner(db):
    return UserDAO.create(db, username="prov-owner", email="prov-owner@example.test")


def _server(db, name="prov-srv"):
    loc = LocationDAO.create(db, name=f"loc-{name}")
    return ServerDAO.create(
        db,
        name=name,
        server_ip="203.0.113.40",
        plugin_name="ipmi",
        plugin_config={},
        location_id=loc.id,
    )


def test_bare_metal_from_group(db_session, monkeypatch):
    owner = _owner(db_session)
    server = _server(db_session)
    group = ServerGroupDAO.create(
        db_session,
        name="prov-pool",
        enable_os_templates=True,
        permitted_os_templates=["ubuntu-cloud-image"],
    )
    group.servers.append(server)
    db_session.commit()

    queued = {}

    def capture(**kwargs):
        queued.update(kwargs)
        return SimpleNamespace(id=1), SimpleNamespace(id=2, boot_task_id=1)

    monkeypatch.setattr(
        "app.api.billing._queue_template_install_for_service", capture
    )

    service = ProvisioningService.create(
        db_session,
        ProvisionRequest(
            name="bm-from-group",
            service_type=ServiceType.BARE_METAL,
            owner_user_id=owner.id,
            server_group_id=group.id,
            template_id="ubuntu-cloud-image",
            provisioning_source=ProvisioningSource.INTERNAL,
        ),
        _actor(),
    )
    assert service.service_type == ServiceType.BARE_METAL
    assert service.bare_metal.server_id == server.id
    assert queued["template_id"] == "ubuntu-cloud-image"


def test_bare_metal_server_id_pin(db_session, monkeypatch):
    owner = _owner(db_session)
    server = _server(db_session, name="pinned-srv")
    monkeypatch.setattr(
        "app.api.billing._queue_template_install_for_service",
        lambda **kwargs: (SimpleNamespace(id=1), SimpleNamespace(id=2, boot_task_id=1)),
    )
    service = ProvisioningService.create(
        db_session,
        ProvisionRequest(
            name="bm-pinned",
            service_type=ServiceType.BARE_METAL,
            owner_user_id=owner.id,
            server_id=server.id,
            provisioning_source=ProvisioningSource.INTERNAL,
        ),
        _actor(),
    )
    assert service.bare_metal.server_id == server.id


def test_bare_metal_requires_group_or_server(db_session):
    owner = _owner(db_session)
    with pytest.raises(ProvisioningError) as exc:
        ProvisioningService.create(
            db_session,
            ProvisionRequest(
                name="bm-missing",
                service_type=ServiceType.BARE_METAL,
                owner_user_id=owner.id,
            ),
            _actor(),
        )
    assert exc.value.code == "invalid_request"


def test_name_taken(db_session):
    owner = _owner(db_session)
    ServiceDAO.create_vm(
        db_session,
        name="taken-svc",
        owner_user_id=owner.id,
        status=ServiceStatus.PENDING,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    with pytest.raises(ProvisioningError) as exc:
        ProvisioningService.create(
            db_session,
            ProvisionRequest(name="taken-svc", service_type=ServiceType.VM),
            _actor(),
        )
    assert exc.value.code == "name_taken"


def test_http_proxy_assigns_ips(db_session, monkeypatch):
    owner = _owner(db_session)
    fake_assignment = SimpleNamespace(id=9)

    monkeypatch.setattr(
        "app.services.provisioning.http_proxy.auto_assign_proxy_ips",
        lambda *a, **k: [fake_assignment],
    )
    service = ProvisioningService.create(
        db_session,
        ProvisionRequest(
            name="px-1",
            service_type=ServiceType.HTTP_PROXY,
            owner_user_id=owner.id,
            ip_count=1,
            provisioning_source=ProvisioningSource.INTERNAL,
        ),
        _actor(),
    )
    assert service.service_type == ServiceType.HTTP_PROXY
    assert service.bare_metal is None or service.bare_metal.server_id is None


def test_vm_creates_child_and_job(db_session, monkeypatch):
    owner = _owner(db_session)
    family = ProductFamilyDAO.create(
        db_session,
        name="VM Fam",
        description=None,
        code="prov-vm-fam",
        service_type="vm",
        provisioning_backend="proxmox",
        defaults={},
        constraints={},
    )
    product = ProductDAO.create(
        db_session,
        family_id=family.id,
        name="VM Prod",
        description=None,
        code="prov-vm-prod",
        overrides={},
    )
    tmpl = VMTemplateDAO.create(
        db_session,
        name="Debian cloud",
        os_type="Linux - Cloudinit",
        proxmox_template_name="ci-debian-prov",
        code="debian-prov",
    )
    ProductDAO.set_vm_templates(db_session, product, [tmpl.id])

    alloc = SimpleNamespace(id=3, ip_address="198.51.100.20")
    monkeypatch.setattr(
        "app.services.provisioning.vm.VMIPAllocationDAO.assign_next_free_to_service",
        lambda *a, **k: alloc,
    )
    monkeypatch.setattr(
        "app.services.provisioning.vm.VMProvisioningService.plan_provisioning",
        lambda **kwargs: {"strategy_name": "linux_cloudinit", "effective_specs": {}},
    )
    scheduled = []
    monkeypatch.setattr(
        "app.services.provisioning.vm.schedule_vm_auto_provision",
        lambda db, service: scheduled.append(service.id),
    )

    service = ProvisioningService.create(
        db_session,
        ProvisionRequest(
            name="vm-from-svc",
            service_type=ServiceType.VM,
            owner_user_id=owner.id,
            product_code=product.code,
            vm_template_id=tmpl.id,
            auto_provision=True,
            provisioning_source=ProvisioningSource.INTERNAL,
        ),
        _actor(),
    )
    assert service.vm is not None
    assert scheduled == [service.id]
    assert (service.config or {}).get("vm_ip_address") == "198.51.100.20"
