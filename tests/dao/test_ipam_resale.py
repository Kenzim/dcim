"""IP reselling: same IP can be assigned to more than one owner up to a
subnet's max_resale_count, but never twice to the same owner_user_id, and
its state only frees once every slot is released."""
import pytest

from app.dao.ipam_dao import IPAMDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.ipam import IPAddress, ServiceIPAssignment
from app.models.service import ProvisioningSource, ServiceType


def _owner(db_session, suffix: str) -> int:
    user = UserDAO.create(db_session, username=f"resale-owner-{suffix}", email=f"resale-{suffix}@example.com")
    return user.id


def _proxy_service(db_session, *, owner_user_id=None, name="proxy-svc") -> int:
    service = ServiceDAO.create_bare_metal(
        db_session,
        name=name,
        owner_user_id=owner_user_id,
        service_type=ServiceType.HTTP_PROXY,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    return service.id


def _single_ip_subnet(db_session, *, name: str, cidr: str, max_resale_count: int = 1):
    return IPAMDAO.create_subnet(
        db_session,
        name=name,
        cidr=cidr,
        range_start=cidr.split("/")[0].rsplit(".", 1)[0] + ".1",
        range_end=cidr.split("/")[0].rsplit(".", 1)[0] + ".1",
        max_resale_count=max_resale_count,
    )


def test_resale_allows_two_different_owners_on_same_ip(db_session):
    subnet = _single_ip_subnet(db_session, name="resale-basic", cidr="10.10.1.0/30", max_resale_count=2)
    owner_a = _owner(db_session, "a")
    owner_b = _owner(db_session, "b")
    svc_a = _proxy_service(db_session, owner_user_id=owner_a, name="svc-a")
    svc_b = _proxy_service(db_session, owner_user_id=owner_b, name="svc-b")

    a1 = IPAMDAO.assign_ip(db_session, service_id=svc_a, subnet_id=subnet.id, username="ua")
    a2 = IPAMDAO.assign_ip(db_session, service_id=svc_b, subnet_id=subnet.id, username="ub")

    assert a1.ip_id == a2.ip_id
    ip_row = db_session.query(IPAddress).filter(IPAddress.id == a1.ip_id).first()
    assert ip_row.state == "assigned"
    assert len(ip_row.assignments) == 2


def test_resale_blocks_same_owner_from_double_holding_ip(db_session):
    subnet = _single_ip_subnet(db_session, name="resale-same-owner", cidr="10.10.2.0/30", max_resale_count=3)
    owner_a = _owner(db_session, "same")
    svc_1 = _proxy_service(db_session, owner_user_id=owner_a, name="svc-1")
    svc_2 = _proxy_service(db_session, owner_user_id=owner_a, name="svc-2")

    IPAMDAO.assign_ip(db_session, service_id=svc_1, subnet_id=subnet.id, username="u1")

    # Same owner_user_id trying to grab a second slot on the *same* IP must
    # be rejected even though the subnet's resale cap (3) has capacity.
    with pytest.raises(ValueError, match="No free IP available"):
        IPAMDAO.assign_ip(db_session, service_id=svc_2, subnet_id=subnet.id, username="u2")


def test_resale_cap_reached_blocks_further_assignment_even_for_new_owner(db_session):
    subnet = _single_ip_subnet(db_session, name="resale-cap-full", cidr="10.10.3.0/30", max_resale_count=2)
    owner_a = _owner(db_session, "cap-a")
    owner_b = _owner(db_session, "cap-b")
    owner_c = _owner(db_session, "cap-c")
    svc_a = _proxy_service(db_session, owner_user_id=owner_a, name="svc-cap-a")
    svc_b = _proxy_service(db_session, owner_user_id=owner_b, name="svc-cap-b")
    svc_c = _proxy_service(db_session, owner_user_id=owner_c, name="svc-cap-c")

    IPAMDAO.assign_ip(db_session, service_id=svc_a, subnet_id=subnet.id)
    IPAMDAO.assign_ip(db_session, service_id=svc_b, subnet_id=subnet.id)

    # Cap (2) is now full; a third, entirely different owner still can't get
    # a slot on this single-IP subnet.
    with pytest.raises(ValueError, match="No free IP available"):
        IPAMDAO.assign_ip(db_session, service_id=svc_c, subnet_id=subnet.id)


def test_release_only_frees_ip_once_last_slot_is_released(db_session):
    subnet = _single_ip_subnet(db_session, name="resale-release", cidr="10.10.4.0/30", max_resale_count=2)
    owner_a = _owner(db_session, "rel-a")
    owner_b = _owner(db_session, "rel-b")
    svc_a = _proxy_service(db_session, owner_user_id=owner_a, name="svc-rel-a")
    svc_b = _proxy_service(db_session, owner_user_id=owner_b, name="svc-rel-b")

    a1 = IPAMDAO.assign_ip(db_session, service_id=svc_a, subnet_id=subnet.id)
    a2 = IPAMDAO.assign_ip(db_session, service_id=svc_b, subnet_id=subnet.id)
    assert a1.ip_id == a2.ip_id

    # Releasing the first of two slots must NOT free the IP — the second
    # owner still holds a live assignment on it.
    assert IPAMDAO.release_ip(db_session, a1.id) is True
    ip_row = db_session.query(IPAddress).filter(IPAddress.id == a1.ip_id).first()
    assert ip_row.state == "assigned"

    # Releasing the last remaining slot does free it.
    assert IPAMDAO.release_ip(db_session, a2.id) is True
    db_session.refresh(ip_row)
    assert ip_row.state == "free"

    # ...and it becomes assignable again (e.g. re-issued to a fresh owner).
    owner_c = _owner(db_session, "rel-c")
    svc_c = _proxy_service(db_session, owner_user_id=owner_c, name="svc-rel-c")
    a3 = IPAMDAO.assign_ip(db_session, service_id=svc_c, subnet_id=subnet.id)
    assert a3.ip_id == a1.ip_id


def test_max_resale_count_defaults_to_one_and_is_exclusive(db_session):
    """A subnet created without specifying max_resale_count reproduces
    today's exclusive-IP behavior exactly."""
    subnet = _single_ip_subnet(db_session, name="resale-default", cidr="10.10.5.0/30")
    assert subnet.max_resale_count == 1
    owner_a = _owner(db_session, "def-a")
    owner_b = _owner(db_session, "def-b")
    svc_a = _proxy_service(db_session, owner_user_id=owner_a, name="svc-def-a")
    svc_b = _proxy_service(db_session, owner_user_id=owner_b, name="svc-def-b")

    IPAMDAO.assign_ip(db_session, service_id=svc_a, subnet_id=subnet.id)
    with pytest.raises(ValueError, match="No free IP available"):
        IPAMDAO.assign_ip(db_session, service_id=svc_b, subnet_id=subnet.id)


def test_create_subnet_rejects_invalid_max_resale_count(db_session):
    with pytest.raises(ValueError, match="max_resale_count"):
        IPAMDAO.create_subnet(db_session, name="bad-resale", cidr="10.10.6.0/30", max_resale_count=0)


def test_update_subnet_rejects_invalid_max_resale_count(db_session):
    subnet = _single_ip_subnet(db_session, name="resale-update", cidr="10.10.7.0/30")
    with pytest.raises(ValueError, match="max_resale_count"):
        IPAMDAO.update_subnet(db_session, subnet.id, max_resale_count=0)


def test_used_slots_reflects_live_assignments_across_resale(db_session):
    subnet = _single_ip_subnet(db_session, name="resale-slots", cidr="10.10.8.0/30", max_resale_count=2)
    owner_a = _owner(db_session, "slots-a")
    owner_b = _owner(db_session, "slots-b")
    svc_a = _proxy_service(db_session, owner_user_id=owner_a, name="svc-slots-a")
    svc_b = _proxy_service(db_session, owner_user_id=owner_b, name="svc-slots-b")

    assert IPAMDAO.used_slots(db_session, subnet.id) == 0
    IPAMDAO.assign_ip(db_session, service_id=svc_a, subnet_id=subnet.id)
    assert IPAMDAO.used_slots(db_session, subnet.id) == 1
    IPAMDAO.assign_ip(db_session, service_id=svc_b, subnet_id=subnet.id)
    assert IPAMDAO.used_slots(db_session, subnet.id) == 2


def test_enforce_resale_cap_race_guard_backs_out_loser(db_session):
    """Simulates two requests that both saw the IP as available before
    either committed (the actual race the cap-check-then-insert flow in
    ``assign_ip`` can hit): the guard keeps the earliest-id assignment and
    raises for the other, restoring the IP to a consistent state."""
    subnet = _single_ip_subnet(db_session, name="resale-race", cidr="10.10.9.0/30", max_resale_count=1)
    ip_row = db_session.query(IPAddress).filter(IPAddress.subnet_id == subnet.id).first()
    owner_a = _owner(db_session, "race-a")
    owner_b = _owner(db_session, "race-b")
    svc_a = _proxy_service(db_session, owner_user_id=owner_a, name="svc-race-a")
    svc_b = _proxy_service(db_session, owner_user_id=owner_b, name="svc-race-b")

    ip_row.state = "assigned"
    winner = ServiceIPAssignment(service_id=svc_a, ip_id=ip_row.id)
    loser = ServiceIPAssignment(service_id=svc_b, ip_id=ip_row.id)
    db_session.add_all([winner, loser])
    db_session.commit()
    db_session.refresh(winner)
    db_session.refresh(loser)

    with pytest.raises(ValueError, match="concurrently assigned at capacity"):
        IPAMDAO._enforce_resale_cap(db_session, ip_row, max_resale=1, assignment=loser)

    remaining = db_session.query(ServiceIPAssignment).filter(ServiceIPAssignment.ip_id == ip_row.id).all()
    assert [a.id for a in remaining] == [winner.id]
    db_session.refresh(ip_row)
    # The winner is still live, so the IP must stay "assigned" — the race
    # guard must not incorrectly free it out from under the survivor.
    assert ip_row.state == "assigned"


def test_enforce_resale_cap_frees_ip_if_loser_was_the_only_survivor(db_session):
    """Defensive branch: if the surviving assignment was somehow already
    gone by the time the guard runs, freeing the IP (rather than leaving it
    stuck 'assigned' with zero assignments) is the safe outcome."""
    subnet = _single_ip_subnet(db_session, name="resale-race-2", cidr="10.10.10.0/30", max_resale_count=1)
    ip_row = db_session.query(IPAddress).filter(IPAddress.subnet_id == subnet.id).first()
    owner_a = _owner(db_session, "race2-a")
    svc_a = _proxy_service(db_session, owner_user_id=owner_a, name="svc-race2-a")

    ip_row.state = "assigned"
    only_assignment = ServiceIPAssignment(service_id=svc_a, ip_id=ip_row.id)
    db_session.add(only_assignment)
    db_session.commit()
    db_session.refresh(only_assignment)

    # max_resale=0 forces the "oversold" branch even with a single
    # assignment, exercising the remaining-count-zero cleanup path.
    with pytest.raises(ValueError, match="concurrently assigned at capacity"):
        IPAMDAO._enforce_resale_cap(db_session, ip_row, max_resale=0, assignment=only_assignment)

    remaining = db_session.query(ServiceIPAssignment).filter(ServiceIPAssignment.ip_id == ip_row.id).all()
    assert remaining == []
    db_session.refresh(ip_row)
    assert ip_row.state == "free"
