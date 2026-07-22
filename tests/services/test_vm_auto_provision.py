"""
Tests for the auto-provision create path helpers in vm_strategy_executor:
placement resolution + VMID reservation, queued marking, background enqueue,
and the idempotency guard. These exercise the shared code path both the admin
and billing VM create endpoints use.
"""
import asyncio

import pytest
from fastapi import BackgroundTasks

from app.dao.service_dao import ServiceDAO
from app.dao.vmid_reservation_dao import VMIDReservationDAO
from app.models.product_catalog import VMTemplate
from app.models.proxmox_inventory import ProxmoxCluster, ProxmoxNode, ProxmoxTemplate
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.service_vm import VMGuestState
from app.services import vm_strategy_executor
from app.services.vm_strategy_executor import (
    mark_vm_provision_queued,
    prepare_vm_placement_for_provisioning,
    provision_vm_service,
    queue_provision_vm_service,
    schedule_vm_auto_provision,
)


def _template(db_session, proxmox_template_name="ubuntu-2204"):
    tmpl = VMTemplate(
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
    # Reservation row now exists for the service (fixes billing asymmetry).
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


def test_schedule_marks_queued_and_enqueues(db_session):
    tmpl = _template(db_session)
    _cluster_with_node(db_session, tmpl.proxmox_template_name)
    service = _vm_service(db_session, tmpl)

    bg = BackgroundTasks()
    schedule_vm_auto_provision(db_session, service, bg)
    db_session.refresh(service)

    assert service.vm.guest_state == VMGuestState.PROVISIONING
    assert (service.config or {}).get("vm_provision", {}).get("status") == "queued"
    # Background task points at the queue entrypoint with the service id.
    assert len(bg.tasks) == 1
    task = bg.tasks[0]
    assert task.func is queue_provision_vm_service
    assert task.args == (service.id,)


def test_schedule_placement_failure_raises(db_session):
    tmpl = _template(db_session)
    # No cluster/node inventory at all -> auto-place cannot resolve.
    service = _vm_service(db_session, tmpl)

    bg = BackgroundTasks()
    with pytest.raises(ValueError):
        schedule_vm_auto_provision(db_session, service, bg)
    assert bg.tasks == []


def test_provision_guard_rejects_running(db_session):
    tmpl = _template(db_session)
    service = _vm_service(db_session, tmpl)
    service.config = {"vm_provision": {"status": "running"}}
    ServiceDAO.update(db_session, service)

    with pytest.raises(ValueError, match="already running"):
        asyncio.run(provision_vm_service(db_session, service.id))


def test_provision_guard_rejects_queued_for_external_caller(db_session):
    tmpl = _template(db_session)
    service = _vm_service(db_session, tmpl)
    service.config = {"vm_provision": {"status": "queued"}}
    ServiceDAO.update(db_session, service)

    # Manual/external caller is rejected while a background job is queued.
    with pytest.raises(ValueError, match="already queued"):
        asyncio.run(provision_vm_service(db_session, service.id))


def test_background_runner_swallows_errors(db_session, monkeypatch):
    """queue_provision_vm_service must never raise into the event loop."""
    # Point the executor's SessionLocal at the test session so the background
    # entrypoint uses the same in-memory DB.
    monkeypatch.setattr(
        vm_strategy_executor, "SessionLocal", lambda: db_session, raising=True
    )
    tmpl = _template(db_session)
    service = _vm_service(db_session, tmpl)  # no placement -> provision will fail

    # Should log and return, not raise.
    asyncio.run(queue_provision_vm_service(service.id))
