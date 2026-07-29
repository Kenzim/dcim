"""
Tests for the auto-provision create path helpers in vm_strategy_executor:
placement resolution + VMID reservation, and enqueuing a durable deployment job
(the execution itself now runs in the deployment worker, not inline). These
exercise the shared code path both the admin and billing VM create endpoints use.
"""
import pytest

from app.dao.service_dao import ServiceDAO
from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.dao.vmid_reservation_dao import VMIDReservationDAO
from app.models.product_catalog import VMTemplate
from app.models.proxmox_inventory import (
    ProxmoxCapacitySnapshot,
    ProxmoxCluster,
    ProxmoxNode,
    ProxmoxTemplate,
)
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.service_vm import VMGuestState
from app.models.vm_deployment_job import DeploymentJobStatus
from app.services.vm_strategy_executor import (
    enqueue_vm_deployment_job,
    prepare_vm_placement_for_provisioning,
    schedule_vm_auto_provision,
)

CLOUDINIT_STEPS = [
    "clone_from_template",
    "configure_sizing",
    "configure_cloudinit_network",
    "power_on",
    "stamp_vm_identity",
]


def _template(db_session, proxmox_template_name="ubuntu-2204"):
    tmpl = VMTemplate(
        code=proxmox_template_name.replace("_", "-"),
        name="Ubuntu 22.04",
        os_type="Linux - Cloudinit",
        proxmox_template_name=proxmox_template_name,
    )
    db_session.add(tmpl)
    db_session.commit()
    db_session.refresh(tmpl)
    return tmpl


def _cluster_with_node(db_session, template_name, node_name="pve1"):
    cluster = ProxmoxCluster(
        name="c1",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
        enabled=True,
        vmid_min=5000,
        vmid_max=5010,
    )
    db_session.add(cluster)
    db_session.commit()
    db_session.refresh(cluster)
    node = ProxmoxNode(cluster_id=cluster.id, node_name=node_name, enabled=True)
    db_session.add(node)
    db_session.commit()
    db_session.refresh(node)
    db_session.add(ProxmoxTemplate(node_id=node.id, vmid=9000, name=template_name))
    db_session.commit()
    return cluster


def _vm_service(db_session, tmpl, cluster_id=None, node=None):
    return ServiceDAO.create_vm(
        db_session,
        name="vm-auto-1",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
        vm_template_id=tmpl.id,
        proxmox_cluster_id=cluster_id,
        proxmox_node_name=node,
    )


def test_prepare_auto_places_and_reserves_vmid(db_session):
    tmpl = _template(db_session)
    cluster = _cluster_with_node(db_session, tmpl.proxmox_template_name)
    service = _vm_service(db_session, tmpl)  # no placement supplied

    prepare_vm_placement_for_provisioning(db_session, service)
    db_session.refresh(service)

    assert service.vm.proxmox_cluster_id == cluster.id
    assert service.vm.proxmox_node_name == "pve1"
    assert 5000 <= service.vm.proxmox_vmid <= 5010
    res = VMIDReservationDAO.get_by_service_id(db_session, service.id)
    assert res is not None
    assert res.vmid == service.vm.proxmox_vmid


def test_prepare_reserves_requested_vmid_when_cluster_given(db_session):
    tmpl = _template(db_session)
    cluster = _cluster_with_node(db_session, tmpl.proxmox_template_name)
    service = _vm_service(db_session, tmpl, cluster_id=cluster.id, node="pve1")
    service.vm.proxmox_vmid = 5005
    ServiceDAO.update(db_session, service)

    prepare_vm_placement_for_provisioning(db_session, service)
    db_session.refresh(service)

    assert service.vm.proxmox_vmid == 5005
    res = VMIDReservationDAO.get_by_service_id(db_session, service.id)
    assert res is not None and res.vmid == 5005


def test_schedule_marks_queued_and_enqueues_job(db_session):
    tmpl = _template(db_session)
    _cluster_with_node(db_session, tmpl.proxmox_template_name)
    service = _vm_service(db_session, tmpl)

    job = schedule_vm_auto_provision(db_session, service)
    db_session.refresh(service)

    assert service.vm.guest_state == VMGuestState.PROVISIONING
    assert (service.config or {}).get("vm_provision", {}).get("status") == "queued"
    # A durable job was created with the resolved strategy + ordered steps.
    assert job.strategy_name == "cloudinit_clone"
    assert job.status == DeploymentJobStatus.QUEUED
    steps = VMDeploymentJobDAO.list_steps(db_session, job.id)
    assert [s.name for s in steps] == CLOUDINIT_STEPS


def test_schedule_placement_failure_raises_and_creates_no_job(db_session):
    tmpl = _template(db_session)
    # No cluster/node inventory at all -> auto-place cannot resolve.
    service = _vm_service(db_session, tmpl)

    with pytest.raises(ValueError):
        schedule_vm_auto_provision(db_session, service)
    assert VMDeploymentJobDAO.list_by_service(db_session, service.id) == []


def test_enqueue_is_idempotent_while_job_active(db_session):
    tmpl = _template(db_session)
    _cluster_with_node(db_session, tmpl.proxmox_template_name)
    service = _vm_service(db_session, tmpl)
    prepare_vm_placement_for_provisioning(db_session, service)

    first = enqueue_vm_deployment_job(db_session, service)
    second = enqueue_vm_deployment_job(db_session, service)

    # Second call returns the same non-terminal job instead of stacking a new one.
    assert first.id == second.id
    assert len(VMDeploymentJobDAO.list_by_service(db_session, service.id)) == 1


def test_enqueue_rejects_stub_strategy(db_session):
    # Template os_type that doesn't map to a strategy -> stub -> rejected.
    tmpl = VMTemplate(code="custom-x", name="Custom", os_type="Unknown", proxmox_template_name="x")
    db_session.add(tmpl)
    db_session.commit()
    db_session.refresh(tmpl)
    service = _vm_service(db_session, tmpl)
    with pytest.raises(ValueError):
        enqueue_vm_deployment_job(db_session, service)


def _cluster_template_on_pve1_pve2_more_ram(db_session, template_name):
    """Template only on pve1; pve2 has more free RAM and no local template copy."""
    cluster = ProxmoxCluster(
        name="c-shared",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
        enabled=True,
        vmid_min=5000,
        vmid_max=5010,
    )
    db_session.add(cluster)
    db_session.commit()
    db_session.refresh(cluster)

    pve1 = ProxmoxNode(cluster_id=cluster.id, node_name="pve1", enabled=True)
    pve2 = ProxmoxNode(cluster_id=cluster.id, node_name="pve2", enabled=True)
    db_session.add_all([pve1, pve2])
    db_session.commit()
    db_session.refresh(pve1)
    db_session.refresh(pve2)

    db_session.add(ProxmoxTemplate(node_id=pve1.id, vmid=9000, name=template_name))
    # pve1: 16 GiB free; pve2: 48 GiB free — shared_storage should prefer pve2.
    db_session.add(
        ProxmoxCapacitySnapshot(
            node_id=pve1.id,
            ram_total_bytes=64 * 1024**3,
            ram_used_bytes=48 * 1024**3,
        )
    )
    db_session.add(
        ProxmoxCapacitySnapshot(
            node_id=pve2.id,
            ram_total_bytes=64 * 1024**3,
            ram_used_bytes=16 * 1024**3,
        )
    )
    db_session.commit()
    return cluster


def test_shared_storage_places_on_any_node_by_free_ram(db_session):
    tmpl = _template(db_session, "ubuntu-shared")
    tmpl.shared_storage = True
    db_session.commit()
    cluster = _cluster_template_on_pve1_pve2_more_ram(db_session, tmpl.proxmox_template_name)
    service = _vm_service(db_session, tmpl)

    prepare_vm_placement_for_provisioning(db_session, service)
    db_session.refresh(service)

    assert service.vm.proxmox_cluster_id == cluster.id
    assert service.vm.proxmox_node_name == "pve2"


def test_without_shared_storage_requires_local_template(db_session):
    tmpl = _template(db_session, "ubuntu-local")
    tmpl.shared_storage = False
    db_session.commit()
    cluster = _cluster_template_on_pve1_pve2_more_ram(db_session, tmpl.proxmox_template_name)
    service = _vm_service(db_session, tmpl)

    prepare_vm_placement_for_provisioning(db_session, service)
    db_session.refresh(service)

    # Only pve1 has the template in inventory, so placement stays on pve1.
    assert service.vm.proxmox_cluster_id == cluster.id
    assert service.vm.proxmox_node_name == "pve1"
