"""Service + service_bare_metal / service_vm extension rows."""

from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.server import Server
from app.models.service import ServiceType, ServiceStatus, ProvisioningSource
from app.services.service_resource import service_linked_server, service_server_id_for_response, vm_placement


def _loc_and_server(db_session):
    loc = Location(name="t-loc", description="t")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    srv = Server(
        name="t-srv",
        server_ip="10.0.0.1",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(srv)
    db_session.commit()
    db_session.refresh(srv)
    return loc, srv


def test_create_bare_metal_service_has_server_link(db_session):
    _, srv = _loc_and_server(db_session)
    s = ServiceDAO.create_bare_metal(
        db_session,
        name="bm-svc",
        server_id=srv.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
    )
    assert s.service_type == ServiceType.BARE_METAL
    assert service_server_id_for_response(s) == srv.id
    assert service_linked_server(db_session, s).id == srv.id
    cid, node, vmid = vm_placement(s)
    assert cid is None and node is None and vmid is None


def test_create_vm_service_has_vm_extension(db_session):
    s = ServiceDAO.create_vm(
        db_session,
        name="vm-svc",
        provisioning_source=ProvisioningSource.INTERNAL,
        proxmox_cluster_id=None,
        proxmox_node_name="pve",
        proxmox_vmid=100,
    )
    assert s.service_type == ServiceType.VM
    assert service_server_id_for_response(s) is None
    assert service_linked_server(db_session, s) is None
    cid, node, vmid = vm_placement(s)
    assert node == "pve" and vmid == 100
