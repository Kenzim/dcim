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


def test_infer_provisioning_source_from_owner(db_session):
    from app.dao.billing_integration_dao import BillingIntegrationDAO
    from app.services.provisioning.service import infer_provisioning_source

    assert infer_provisioning_source(db_session, None) == ProvisioningSource.INTERNAL
    internal = _owner(db_session)
    assert infer_provisioning_source(db_session, internal.id) == ProvisioningSource.INTERNAL
    with pytest.raises(ProvisioningError) as exc:
        infer_provisioning_source(db_session, 999999)
    assert exc.value.code == "not_found"
    integration = BillingIntegrationDAO.create(
        db_session, name="prov-bill", integration_type="whmcs"
    )
    billed = UserDAO.create(
        db_session,
        username="prov-billed",
        email="prov-billed@example.test",
        billing_integration_id=integration.id,
        external_user_id="prov-billed",
    )
    assert infer_provisioning_source(db_session, billed.id) == ProvisioningSource.BILLING


def test_catalog_defaults_fill_bare_metal_group(db_session, monkeypatch):
    owner = _owner(db_session)
    server = _server(db_session, name="catalog-bm-srv")
    group = ServerGroupDAO.create(
        db_session,
        name="catalog-bm-pool",
        enable_os_templates=True,
        permitted_os_templates=["ubuntu-cloud-image"],
    )
    group.servers.append(server)
    db_session.commit()
    family = ProductFamilyDAO.create(
        db_session,
        name="BM Fam",
        description=None,
        code="prov-bm-fam",
        service_type="bare_metal",
        provisioning_backend="ipmi",
        defaults={"server_group_id": group.id},
        constraints={},
    )
    ProductDAO.create(
        db_session,
        family_id=family.id,
        name="BM Prod",
        description=None,
        code="prov-bm-prod",
        overrides={},
    )
    monkeypatch.setattr(
        "app.api.billing._queue_template_install_for_service",
        lambda **kwargs: (SimpleNamespace(id=1), SimpleNamespace(id=2, boot_task_id=1)),
    )
    service = ProvisioningService.create(
        db_session,
        ProvisionRequest(
            name="bm-from-catalog",
            service_type=ServiceType.BARE_METAL,
            owner_user_id=owner.id,
            product_code="prov-bm-prod",
            provisioning_source=ProvisioningSource.INTERNAL,
        ),
        _actor(),
    )
    assert service.bare_metal.server_id == server.id


def test_vm_catalog_defaults_template_and_cluster(db_session, monkeypatch):
    owner = _owner(db_session)
    from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO

    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="prov-cluster",
        api_url="https://pve.example:8006/",
        username="root@pam",
        password="x",
    )
    family = ProductFamilyDAO.create(
        db_session,
        name="VM Fam2",
        description=None,
        code="prov-vm-fam2",
        service_type="vm",
        provisioning_backend="proxmox",
        defaults={"proxmox_cluster_id": cluster.id},
        constraints={},
    )
    product = ProductDAO.create(
        db_session,
        family_id=family.id,
        name="VM Prod2",
        description=None,
        code="prov-vm-prod2",
        overrides={},
    )
    tmpl = VMTemplateDAO.create(
        db_session,
        name="Ubuntu cloud",
        os_type="Linux - Cloudinit",
        proxmox_template_name="ci-ubuntu-prov",
        code="ubuntu-prov",
    )
    ProductDAO.set_vm_templates(db_session, product, [tmpl.id])
    monkeypatch.setattr(
        "app.services.provisioning.vm.VMIPAllocationDAO.assign_next_free_to_service",
        lambda *a, **k: SimpleNamespace(id=8, ip_address="198.51.100.21"),
    )
    monkeypatch.setattr(
        "app.services.provisioning.vm.VMProvisioningService.plan_provisioning",
        lambda **kwargs: {"strategy_name": "linux_cloudinit", "effective_specs": {}},
    )
    service = ProvisioningService.create(
        db_session,
        ProvisionRequest(
            name="vm-from-catalog",
            service_type=ServiceType.VM,
            owner_user_id=owner.id,
            product_code=product.code,
            auto_provision=False,
            provisioning_source=ProvisioningSource.INTERNAL,
        ),
        _actor(),
    )
    assert service.vm is not None
    assert service.vm.vm_template_id == tmpl.id
    assert service.vm.proxmox_cluster_id == cluster.id


def test_vm_rejects_vmid_without_cluster_and_unknown_cluster(db_session):
    owner = _owner(db_session)
    with pytest.raises(ProvisioningError) as exc:
        ProvisioningService.create(
            db_session,
            ProvisionRequest(
                name="vmid-no-cluster",
                service_type=ServiceType.VM,
                owner_user_id=owner.id,
                proxmox_vmid=100,
            ),
            _actor(),
        )
    assert exc.value.code == "invalid_request"

    with pytest.raises(ProvisioningError) as exc:
        ProvisioningService.create(
            db_session,
            ProvisionRequest(
                name="bad-cluster",
                service_type=ServiceType.VM,
                owner_user_id=owner.id,
                proxmox_cluster_id=99999,
            ),
            _actor(),
        )
    assert exc.value.code == "not_found"


def test_vm_auto_provision_conflict_sets_error(db_session, monkeypatch):
    owner = _owner(db_session)
    family = ProductFamilyDAO.create(
        db_session,
        name="VM Fam3",
        description=None,
        code="prov-vm-fam3",
        service_type="vm",
        provisioning_backend="proxmox",
        defaults={},
        constraints={},
    )
    product = ProductDAO.create(
        db_session,
        family_id=family.id,
        name="VM Prod3",
        description=None,
        code="prov-vm-prod3",
        overrides={},
    )
    tmpl = VMTemplateDAO.create(
        db_session,
        name="Alpine",
        os_type="Linux - Cloudinit",
        proxmox_template_name="ci-alpine-prov",
        code="alpine-prov",
    )
    ProductDAO.set_vm_templates(db_session, product, [tmpl.id])
    monkeypatch.setattr(
        "app.services.provisioning.vm.VMIPAllocationDAO.assign_next_free_to_service",
        lambda *a, **k: SimpleNamespace(id=11, ip_address="198.51.100.22"),
    )
    monkeypatch.setattr(
        "app.services.provisioning.vm.VMProvisioningService.plan_provisioning",
        lambda **kwargs: {"strategy_name": "linux_cloudinit", "effective_specs": {}},
    )

    def boom(db, service):
        raise ValueError("already queued")

    monkeypatch.setattr("app.services.provisioning.vm.schedule_vm_auto_provision", boom)
    with pytest.raises(ProvisioningError) as exc:
        ProvisioningService.create(
            db_session,
            ProvisionRequest(
                name="vm-auto-conflict",
                service_type=ServiceType.VM,
                owner_user_id=owner.id,
                product_code=product.code,
                vm_template_id=tmpl.id,
                auto_provision=True,
                provisioning_source=ProvisioningSource.INTERNAL,
            ),
            _actor(),
        )
    assert exc.value.code == "conflict"
