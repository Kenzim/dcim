"""Tests for VM IP browse / reassign helpers."""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.services.vm_ip_reassign import (
    VmIpReassignError,
    list_available_ips_for_service,
    reassign_vm_ip,
)


def _cluster(db_session, name="c1"):
    c = ProxmoxCluster(
        name=name,
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


def _pool_ip(db_session, ip, cluster_ids=None, enabled=True):
    row = VMIPAllocationDAO.create(
        db_session,
        ip_address_value=ip,
        subnet_mask="255.255.255.0",
        gateway="10.20.30.1",
        bridge_name="vmbr0",
        cluster_ids=cluster_ids or [],
        enabled=enabled,
    )
    db_session.commit()
    db_session.refresh(row)
    return row


def test_list_available_ips_filters_by_cluster(db_session):
    c1 = _cluster(db_session, "london")
    c2 = _cluster(db_session, "other")
    current = _pool_ip(db_session, "10.20.30.10", [c1.id])
    free_ok = _pool_ip(db_session, "10.20.30.11", [c1.id])
    _pool_ip(db_session, "10.20.30.12", [c2.id])  # wrong cluster
    _pool_ip(db_session, "10.20.30.13", [])  # unrestricted — usable
    _pool_ip(db_session, "10.20.30.14", [c1.id], enabled=False)

    service = ServiceDAO.create_vm(
        db_session,
        name="vm-ip-list",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=c1.id,
    )
    VMIPAllocationDAO.assign_specific_to_service(
        db_session,
        service_id=service.id,
        allocation_id=current.id,
        proxmox_cluster_id=c1.id,
    )
    db_session.commit()
    db_session.refresh(service)

    payload = list_available_ips_for_service(db_session, service)
    assert payload["proxmox_cluster_id"] == c1.id
    assert payload["current"]["ip_address"] == "10.20.30.10"
    ips = {row["ip_address"] for row in payload["available"]}
    assert ips == {"10.20.30.11", "10.20.30.13"}
    assert free_ok.ip_address in ips


def test_assign_specific_rejects_taken_ip(db_session):
    c = _cluster(db_session)
    a = _pool_ip(db_session, "10.20.30.20", [c.id])
    s1 = ServiceDAO.create_vm(
        db_session,
        name="owner",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=c.id,
    )
    s2 = ServiceDAO.create_vm(
        db_session,
        name="other",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=c.id,
    )
    VMIPAllocationDAO.assign_specific_to_service(
        db_session, service_id=s1.id, allocation_id=a.id, proxmox_cluster_id=c.id
    )
    db_session.commit()
    with pytest.raises(ValueError, match="already assigned"):
        VMIPAllocationDAO.assign_specific_to_service(
            db_session, service_id=s2.id, allocation_id=a.id, proxmox_cluster_id=c.id
        )


def test_reassign_vm_ip_releases_old_and_resets_network(db_session, monkeypatch):
    c = _cluster(db_session)
    old = _pool_ip(db_session, "10.20.30.30", [c.id])
    new = _pool_ip(db_session, "10.20.30.31", [c.id])
    service = ServiceDAO.create_vm(
        db_session,
        name="vm-reassign",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=c.id,
    )
    VMIPAllocationDAO.assign_specific_to_service(
        db_session,
        service_id=service.id,
        allocation_id=old.id,
        proxmox_cluster_id=c.id,
    )
    service.config = {
        "vm_ip_allocation_id": old.id,
        "vm_ip_address": old.ip_address,
    }
    ServiceDAO.update(db_session, service)
    db_session.commit()
    db_session.refresh(service)

    reset_calls = []

    async def fake_run_action(db, svc, action_name, params, audience):
        reset_calls.append((action_name, audience, svc.id))
        return {"status": "ok", "action": action_name, "via": "cloudinit"}

    monkeypatch.setattr(
        "app.services.vm_ip_reassign.run_action",
        fake_run_action,
    )

    result = asyncio.run(
        reassign_vm_ip(
            db_session,
            service,
            allocation_id=new.id,
            reset_network=True,
            source="test",
        )
    )
    assert result["status"] == "ok"
    assert result["old_ip_address"] == "10.20.30.30"
    assert result["vm_ip_address"] == "10.20.30.31"
    assert reset_calls == [("reset_network", "admin", service.id)]

    db_session.refresh(old)
    db_session.refresh(new)
    db_session.refresh(service)
    assert old.assigned_service_id is None
    assert new.assigned_service_id == service.id
    assert service.vm.vm_ip_allocation_id == new.id
    assert service.config["vm_ip_address"] == "10.20.30.31"


def test_reassign_same_ip_errors(db_session):
    c = _cluster(db_session)
    row = _pool_ip(db_session, "10.20.30.40", [c.id])
    service = ServiceDAO.create_vm(
        db_session,
        name="same-ip",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=c.id,
    )
    VMIPAllocationDAO.assign_specific_to_service(
        db_session,
        service_id=service.id,
        allocation_id=row.id,
        proxmox_cluster_id=c.id,
    )
    db_session.commit()
    db_session.refresh(service)

    with pytest.raises(VmIpReassignError, match="already assigned"):
        asyncio.run(
            reassign_vm_ip(db_session, service, allocation_id=row.id, reset_network=False)
        )


def test_list_available_rejects_non_vm():
    svc = MagicMock()
    svc.service_type = ServiceType.BARE_METAL
    svc.vm = None
    with pytest.raises(VmIpReassignError, match="only listed for VM"):
        list_available_ips_for_service(MagicMock(), svc)
