import pytest

from app.dao.ipam_dao import IPAMDAO
from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.server import Server
from app.models.service import ProvisioningSource
from app.models.ipam import IPAddress, ServiceIPAssignmentHistory


def _create_service(db_session) -> int:
    location = Location(name="IPAM DAO Location", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name="ipam-dao-server",
        server_ip="192.168.50.10",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"host": "192.168.50.10"},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    service = ServiceDAO.create_bare_metal(
        db_session,
        name="ipam-service",
        server_id=server.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    return service.id


def test_create_subnet_seeds_ip_range_and_queries(db_session):
    subnet = IPAMDAO.create_subnet(
        db_session,
        name="test-subnet",
        cidr="10.0.0.0/29",
        range_start="10.0.0.2",
        range_end="10.0.0.4",
    )
    assert subnet.id is not None

    all_subnets = IPAMDAO.list_subnets(db_session)
    assert len(all_subnets) == 1
    assert IPAMDAO.get_subnet(db_session, subnet.id).cidr == "10.0.0.0/29"

    ips = db_session.query(IPAddress).filter(IPAddress.subnet_id == subnet.id).order_by(IPAddress.ip_address).all()
    ip_values = [row.ip_address for row in ips]
    assert ip_values == ["10.0.0.2", "10.0.0.3", "10.0.0.4"]


def test_assign_release_and_history(db_session):
    service_id = _create_service(db_session)
    subnet = IPAMDAO.create_subnet(db_session, name="assign-subnet", cidr="10.0.1.0/30")

    assignment = IPAMDAO.assign_ip(
        db_session,
        service_id=service_id,
        subnet_id=subnet.id,
        username="root",
        assigned_by="tester",
    )
    assert assignment.id is not None
    assert assignment.service_id == service_id

    assignments = IPAMDAO.get_assignment_by_service(db_session, service_id)
    assert len(assignments) == 1
    assert assignments[0].id == assignment.id

    assigned_ip = assignment.ip.ip_address
    assigned_row = db_session.query(IPAddress).filter(IPAddress.id == assignment.ip_id).first()
    assert assigned_row.state == "assigned"

    history = IPAMDAO.list_history(db_session, service_id=service_id)
    assert len(history) == 1
    assert history[0].action == "assigned"
    assert history[0].ip_address == assigned_ip
    assert history[0].created_at is not None

    released = IPAMDAO.release_ip(db_session, assignment.id, released_by="tester-2")
    assert released is True

    reassigned_row = db_session.query(IPAddress).filter(IPAddress.id == assignment.ip_id).first()
    assert reassigned_row.state == "free"

    history = IPAMDAO.list_history(db_session, service_id=service_id)
    actions = [h.action for h in history]
    assert "assigned" in actions and "released" in actions

    assert IPAMDAO.release_ip(db_session, 99999) is False


def test_assign_ip_errors_for_disabled_or_missing_capacity(db_session):
    service_id = _create_service(db_session)
    subnet = IPAMDAO.create_subnet(db_session, name="disabled-subnet", cidr="10.0.2.0/30")
    subnet.enabled = False
    db_session.commit()

    with pytest.raises(ValueError, match="No enabled subnets available"):
        IPAMDAO.assign_ip(db_session, service_id=service_id, subnet_id=subnet.id)

    enabled_subnet = IPAMDAO.create_subnet(db_session, name="full-subnet", cidr="10.0.3.0/30")
    all_ip_ids = [ip.id for ip in db_session.query(IPAddress).filter(IPAddress.subnet_id == enabled_subnet.id).all()]
    for ip_id in all_ip_ids:
        row = db_session.query(IPAddress).filter(IPAddress.id == ip_id).first()
        row.state = "assigned"
    db_session.commit()

    with pytest.raises(ValueError, match="No free IP available"):
        IPAMDAO.assign_ip(db_session, service_id=service_id, subnet_id=enabled_subnet.id)

