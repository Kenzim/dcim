"""VM IP pool assignment rules for services."""

from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import ProvisioningSource, ServiceStatus


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


def test_assign_global_pool_when_no_cluster(db_session):
    VMIPAllocationDAO.create(
        db_session,
        ip_address_value="10.20.30.40",
        subnet_mask="255.255.255.0",
        gateway="10.20.30.1",
        bridge_name="vmbr0",
        cluster_ids=[],
        enabled=True,
    )
    db_session.commit()

    s = ServiceDAO.create_vm(
        db_session,
        name="vm-a",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
    )
    alloc = VMIPAllocationDAO.assign_next_free_to_service(
        db_session, service_id=s.id, proxmox_cluster_id=None
    )
    assert alloc is not None
    assert alloc.ip_address == "10.20.30.40"
    assert alloc.assigned_service_id == s.id
    db_session.refresh(s)
    assert s.vm is not None
    assert s.vm.vm_ip_allocation_id == alloc.id
    db_session.commit()


def test_skip_cluster_restricted_pool_when_service_has_no_cluster(db_session):
    c = _cluster(db_session)
    VMIPAllocationDAO.create(
        db_session,
        ip_address_value="10.20.30.41",
        subnet_mask="255.255.255.0",
        gateway="10.20.30.1",
        bridge_name="vmbr0",
        cluster_ids=[c.id],
        enabled=True,
    )
    db_session.commit()

    s = ServiceDAO.create_vm(
        db_session,
        name="vm-b",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
    )
    alloc = VMIPAllocationDAO.assign_next_free_to_service(
        db_session, service_id=s.id, proxmox_cluster_id=None
    )
    assert alloc is None


def test_assign_cluster_pool_when_cluster_matches(db_session):
    c = _cluster(db_session)
    VMIPAllocationDAO.create(
        db_session,
        ip_address_value="10.20.30.42",
        subnet_mask="255.255.255.0",
        gateway="10.20.30.1",
        bridge_name="vmbr0",
        cluster_ids=[c.id],
        enabled=True,
    )
    db_session.commit()

    s = ServiceDAO.create_vm(
        db_session,
        name="vm-c",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
        proxmox_cluster_id=c.id,
    )
    alloc = VMIPAllocationDAO.assign_next_free_to_service(
        db_session, service_id=s.id, proxmox_cluster_id=c.id
    )
    assert alloc is not None
    assert alloc.assigned_service_id == s.id


def test_release_for_service(db_session):
    VMIPAllocationDAO.create(
        db_session,
        ip_address_value="10.20.30.43",
        subnet_mask="255.255.255.0",
        gateway="10.20.30.1",
        bridge_name="vmbr0",
        cluster_ids=[],
        enabled=True,
    )
    db_session.commit()

    s = ServiceDAO.create_vm(
        db_session,
        name="vm-d",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
    )
    alloc = VMIPAllocationDAO.assign_next_free_to_service(
        db_session, service_id=s.id, proxmox_cluster_id=None
    )
    assert alloc is not None
    db_session.commit()

    n = VMIPAllocationDAO.release_for_service(db_session, s.id)
    assert n == 1
    db_session.commit()
    db_session.refresh(alloc)
    assert alloc.assigned_service_id is None
    db_session.refresh(s)
    assert s.vm.vm_ip_allocation_id is None
