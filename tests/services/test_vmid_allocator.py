from app.dao.service_dao import ServiceDAO
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import ProvisioningSource, ServiceStatus
from app.services.vmid_allocator import reserve_vmid_for_service


def test_vmid_allocator_uses_cluster_range_and_never_reuses(db_session):
    cluster = ProxmoxCluster(
        name="c1",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
        vmid_min=5000,
        vmid_max=5002,
    )
    db_session.add(cluster)
    db_session.commit()
    db_session.refresh(cluster)

    s1 = ServiceDAO.create_vm(
        db_session,
        name="vm-alloc-1",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
        proxmox_cluster_id=cluster.id,
    )
    vmid1 = reserve_vmid_for_service(db_session, cluster_id=cluster.id, service_id=s1.id)
    assert vmid1 == 5000

    # Simulate service termination - reservation must still block reuse.
    s1.status = ServiceStatus.TERMINATED
    db_session.commit()

    s2 = ServiceDAO.create_vm(
        db_session,
        name="vm-alloc-2",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
        proxmox_cluster_id=cluster.id,
    )
    vmid2 = reserve_vmid_for_service(db_session, cluster_id=cluster.id, service_id=s2.id)
    assert vmid2 == 5001
    assert vmid2 != vmid1
