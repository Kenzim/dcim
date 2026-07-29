"""IPAM hardening: oversized CIDR seeding is rejected, spread_subnets actually
spreads load across subnets (not just even/odd within one), and a concurrent
double-assign of the same IP surfaces a clean retryable ValueError instead of
an unhandled IntegrityError/500.
"""
import pytest

from app.dao.ipam_dao import IPAMDAO, MAX_SEED_HOSTS
from app.dao.service_dao import ServiceDAO
from app.models.ipam import IPAddress, ServiceIPAssignment
from app.models.location import Location
from app.models.server import Server
from app.models.service import ProvisioningSource


def _create_service(db_session, suffix="1") -> int:
    location = Location(name=f"IPAM Hardening Loc {suffix}", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name=f"ipam-hardening-server-{suffix}",
        server_ip=f"192.168.60.{suffix}",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    service = ServiceDAO.create_bare_metal(
        db_session,
        name=f"ipam-hardening-service-{suffix}",
        server_id=server.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    return service.id


def test_create_subnet_rejects_oversized_cidr(db_session):
    with pytest.raises(ValueError, match="exceeding"):
        IPAMDAO.create_subnet(db_session, name="huge-subnet", cidr="10.10.0.0/16")

    # Nothing should have been persisted from the rejected attempt.
    assert IPAMDAO.list_subnets(db_session) == []


def test_create_subnet_allows_large_cidr_with_narrow_range(db_session):
    # A /16 is normally rejected, but a range_start/range_end narrow enough
    # to stay under MAX_SEED_HOSTS must still be allowed.
    subnet = IPAMDAO.create_subnet(
        db_session,
        name="narrowed-subnet",
        cidr="10.20.0.0/16",
        range_start="10.20.0.1",
        range_end="10.20.0.5",
    )
    ips = db_session.query(IPAddress).filter(IPAddress.subnet_id == subnet.id).all()
    assert len(ips) == 5
    assert len(ips) < MAX_SEED_HOSTS


def test_spread_subnets_prefers_least_loaded_subnet(db_session):
    service_id = _create_service(db_session, suffix="2")
    busy = IPAMDAO.create_subnet(db_session, name="busy-subnet", cidr="10.30.0.0/29")
    quiet = IPAMDAO.create_subnet(db_session, name="quiet-subnet", cidr="10.30.1.0/29")

    # Load up "busy" first so it has more assigned IPs than "quiet".
    for _ in range(3):
        IPAMDAO.assign_ip(db_session, service_id=service_id, subnet_id=busy.id, strategy="spread_subnets")

    # Now let the strategy pick a subnet on its own (no subnet_id pinned) —
    # it must prefer the quieter subnet instead of draining "busy" further.
    assignment = IPAMDAO.assign_ip(db_session, service_id=service_id, strategy="spread_subnets")
    assert assignment.ip.subnet_id == quiet.id


def test_concurrent_assign_race_surfaces_clean_value_error(db_session):
    service_a_id = _create_service(db_session, suffix="3")
    service_b_id = _create_service(db_session, suffix="4")
    subnet = IPAMDAO.create_subnet(db_session, name="race-subnet", cidr="10.40.0.0/30")
    ip_row = db_session.query(IPAddress).filter(IPAddress.subnet_id == subnet.id).first()

    # Simulate a competing transaction that already inserted an assignment
    # for this IP but hasn't flipped ip.state yet (still "free" from this
    # session's point of view) — the DAO must not let a second assignment
    # through to a 500; it should hit the unique constraint and translate
    # it into a clean, retryable ValueError.
    db_session.add(ServiceIPAssignment(service_id=service_a_id, ip_id=ip_row.id, username="first"))
    db_session.commit()

    with pytest.raises(ValueError, match="concurrently assigned"):
        IPAMDAO.assign_ip(db_session, service_id=service_b_id, subnet_id=subnet.id)

    # The session must still be usable after the rollback inside assign_ip.
    assert IPAMDAO.list_subnets(db_session)[0].id == subnet.id
