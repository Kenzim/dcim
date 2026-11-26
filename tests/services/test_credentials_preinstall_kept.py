"""Keys of an install started on a spare server survive the creation of the customer's service.

Keys are kept only when the install began after the server's previous service ended and
no other live service is on the server; anything else is an earlier customer's leftover.
"""
from datetime import datetime, timedelta, timezone

import pytest

import app.api.billing as billing
import app.services.provisioning.bare_metal as bare_metal
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.services.provisioning.errors import ProvisioningError
from app.services.provisioning.request import ProvisionRequest, ProvisioningActor
from app.services.server_credentials import clear_credentials_for_new_allocation
from tests.api.test_billing_credentials_isolation import (  # noqa: F401  (_install_mocks is autouse)
    UBUNTU,
    UBUNTU_PARAMS,
    _admin_install,
    _install_mocks,
    _server,
    _service,
    _user,
)

ACTOR = ProvisioningActor(kind="admin", actor_id=1, name="test", source="test")
NOW = datetime.now(timezone.utc)


def _keys(installed_at):
    return {
        "os_type": "linux",
        "template_id": UBUNTU.id,
        "last_updated": installed_at.isoformat() if installed_at else None,
        "username": "someone",
        "password": "LEFTOVER-SECRET",
    }


def _ended_service(db, server, ended_at, name="previous"):
    service = ServiceDAO.create_bare_metal(
        db,
        name=f"{server.name}-{name}",
        server_id=server.id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.TERMINATED,
        provisioning_source=ProvisioningSource.INTERNAL,
        config={},
    )
    service.terminated_at = ended_at
    db.commit()
    return service


def _create(db, server, name="new-customer"):
    request = ProvisionRequest(
        name=name,
        service_type=ServiceType.BARE_METAL,
        provisioning_source=ProvisioningSource.INTERNAL,
        server_id=server.id,
    )
    return bare_metal.create_bare_metal_service(db, request, ACTOR, {})


def _stored(db, server):
    db.refresh(server)
    return server.credentials


def test_preinstall_on_a_spare_server_is_kept_and_reaches_the_new_service(db_session):
    server = _server(db_session, "pre-a", "198.51.100.160")
    _admin_install(db_session, server, UBUNTU.id, dict(UBUNTU_PARAMS))
    service = _create(db_session, server)
    assert _stored(db_session, server)["password"] == UBUNTU_PARAMS["password"]
    payload = billing._billing_service_response(db_session, service)
    assert payload.credentials["password"] == UBUNTU_PARAMS["password"]
    assert payload.credentials["username"] == UBUNTU_PARAMS["username"]


def test_preinstall_after_the_previous_service_ended_is_kept(db_session):
    server = _server(db_session, "pre-b", "198.51.100.161")
    _ended_service(db_session, server, NOW - timedelta(days=1))
    _admin_install(db_session, server, UBUNTU.id, dict(UBUNTU_PARAMS))
    _create(db_session, server)
    assert _stored(db_session, server)["password"] == UBUNTU_PARAMS["password"]


def test_keys_installed_before_the_previous_service_ended_are_cleared(db_session):
    server = _server(db_session, "pre-c", "198.51.100.162")
    server.credentials = _keys(NOW - timedelta(days=3))
    db_session.commit()
    _ended_service(db_session, server, NOW - timedelta(days=1))
    service = _create(db_session, server)
    assert not _stored(db_session, server)
    assert billing._billing_service_response(db_session, service).credentials is None


def test_keys_from_an_install_that_began_exactly_when_the_service_ended_are_cleared(db_session):
    server = _server(db_session, "pre-d", "198.51.100.163")
    server.credentials = _keys(NOW - timedelta(days=1))
    db_session.commit()
    _ended_service(db_session, server, NOW - timedelta(days=1))
    _create(db_session, server)
    assert not _stored(db_session, server)


def test_previous_service_without_an_end_time_clears(db_session):
    server = _server(db_session, "pre-e", "198.51.100.164")
    server.credentials = _keys(NOW)
    db_session.commit()
    _ended_service(db_session, server, None)
    _create(db_session, server)
    assert not _stored(db_session, server)


@pytest.mark.parametrize("installed_at", [None, "not a date"])
def test_never_used_server_with_unknown_install_time_is_cleared(db_session, installed_at):
    server = _server(db_session, f"pre-f-{bool(installed_at)}", f"198.51.100.{170 + bool(installed_at)}")
    keys = _keys(None)
    if installed_at:
        keys["last_updated"] = installed_at
    server.credentials = keys
    db_session.commit()
    _create(db_session, server)
    assert not _stored(db_session, server)


def test_never_used_server_with_a_known_install_time_is_kept(db_session):
    server = _server(db_session, "pre-g", "198.51.100.172")
    server.credentials = _keys(NOW - timedelta(hours=2))
    db_session.commit()
    _create(db_session, server)
    assert _stored(db_session, server)["password"] == "LEFTOVER-SECRET"


def test_refused_allocation_of_a_live_server_leaves_the_live_service_view_unchanged(db_session):
    integration = BillingIntegrationDAO.create(db_session, name="pre-h", integration_type="whmcs")
    server = _server(db_session, "pre-h-srv", "198.51.100.165")
    live = _service(db_session, server, _user(db_session, integration, "pre-h-u"), "pre-h-live")
    _admin_install(db_session, server, UBUNTU.id, dict(UBUNTU_PARAMS))
    before = billing._billing_service_response(db_session, live).credentials
    with pytest.raises(ProvisioningError):
        _create(db_session, server)
    after = billing._billing_service_response(db_session, live).credentials
    assert before["password"] == UBUNTU_PARAMS["password"]
    assert after == before


def test_keys_are_never_handed_over_while_another_service_is_live(db_session):
    integration = BillingIntegrationDAO.create(db_session, name="pre-i", integration_type="whmcs")
    server = _server(db_session, "pre-i-srv", "198.51.100.166")
    _service(db_session, server, _user(db_session, integration, "pre-i-u"), "pre-i-live")
    server.credentials = _keys(NOW + timedelta(hours=1))
    db_session.commit()
    clear_credentials_for_new_allocation(db_session, server)
    assert not server.credentials


def test_fresh_keys_on_a_server_with_a_live_service_are_cleared_by_the_helper(db_session):
    integration = BillingIntegrationDAO.create(db_session, name="pre-j", integration_type="whmcs")
    server = _server(db_session, "pre-j-srv", "198.51.100.167")
    _service(db_session, server, _user(db_session, integration, "pre-j-u"), "pre-j-live")
    server.credentials = _keys(datetime.now(timezone.utc))
    db_session.commit()
    clear_credentials_for_new_allocation(db_session, server)
    assert not server.credentials


MARCH = datetime(2026, 3, 1, tzinfo=timezone.utc)
MAY = datetime(2026, 5, 15, tzinfo=timezone.utc)
JUNE = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("first_ended, second_ended", [(MARCH, JUNE), (JUNE, MARCH)], ids=["march-first", "june-first"])
def test_keys_written_between_two_earlier_services_ends_are_cleared_in_either_order(
    db_session, first_ended, second_ended
):
    server = _server(db_session, f"pre-k-{first_ended.month}", f"198.51.100.{180 + first_ended.month}")
    _ended_service(db_session, server, first_ended, name="first")
    _ended_service(db_session, server, second_ended, name="second")
    server.credentials = _keys(MAY)
    db_session.commit()
    _create(db_session, server)
    assert not _stored(db_session, server)


def test_install_in_the_same_second_as_the_end_is_cleared_to_the_second(db_session):
    """Times compare at whole seconds, as the database stores terminated_at without a fraction."""
    server = _server(db_session, "pre-sec", "198.51.100.170")
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    server.credentials = _keys(base.replace(microsecond=750000))
    db_session.commit()
    _ended_service(db_session, server, base.replace(microsecond=250000))
    _create(db_session, server)
    assert not _stored(db_session, server)


@pytest.mark.parametrize(
    "installed_at",
    ["2026-06-01T12:30:00+01:00", "2026-06-01T14:00:00+01:00"],
    ids=["before-the-end-in-utc", "after-the-end-in-utc"],
)
def test_install_time_with_a_non_utc_offset_is_cleared(db_session, installed_at):
    """The writers only ever stamp +00:00; any other offset is unknown, as in the clean-up SQL, so it clears.

    The first value is 11:30 UTC, before the 12:00 UTC end, and would be kept if read as 12:30 UTC.
    """
    server = _server(db_session, f"pre-off-{installed_at[11:13]}", f"198.51.100.{171 + (int(installed_at[11:13]) - 12) // 2}")
    keys = _keys(NOW)
    keys["last_updated"] = installed_at
    server.credentials = keys
    db_session.commit()
    _ended_service(db_session, server, datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
    _create(db_session, server)
    assert not _stored(db_session, server)
