"""A server allocated to a new service starts with no stored OS credentials.

A retried order (resume) and a reinstall of the same service are not new
allocations and must leave the column alone.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app.services.provisioning.bare_metal as bare_metal
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.services.provisioning.errors import ProvisioningError
from app.services.provisioning.request import ProvisionRequest, ProvisioningActor
from tests.api.test_billing_credentials_isolation import _install_mocks  # noqa: F401  (autouse fixture)

ACTOR = ProvisioningActor(kind="admin", actor_id=1, name="test", source="test")
STALE = {
    "os_type": "linux",
    "template_id": "ubuntu-cloud-image",
    "last_updated": "2026-01-01T00:00:00",
    "username": "previous",
    "password": "PREVIOUS-SECRET",
}


@pytest.fixture
def clear_calls(monkeypatch):
    """Records calls to the allocation clear helper, whatever its name resolves to."""
    calls = []
    original = getattr(bare_metal, "clear_credentials_for_new_allocation", None)
    if original is not None:

        def recording(*args):
            calls.append(args[-1].id)
            return original(*args)

        monkeypatch.setattr(bare_metal, "clear_credentials_for_new_allocation", recording)
    return calls


def _server(db, name, ip, previous_customer=True):
    """A server holding STALE keys, installed in January for a service that ended in June."""
    loc = LocationDAO.create(db, name=f"{name}-loc")
    server = ServerDAO.create(
        db, name=name, server_ip=ip, plugin_name="ipmi", plugin_config={}, location_id=loc.id
    )
    server.credentials = dict(STALE)
    db.commit()
    if previous_customer:
        old = ServiceDAO.create_bare_metal(
            db,
            name=f"{name}-previous",
            server_id=server.id,
            service_type=ServiceType.BARE_METAL,
            status=ServiceStatus.TERMINATED,
            provisioning_source=ProvisioningSource.INTERNAL,
            config={},
        )
        old.terminated_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
        db.commit()
    return server


def _request(name, **extra):
    return ProvisionRequest(
        name=name,
        service_type=ServiceType.BARE_METAL,
        provisioning_source=ProvisioningSource.INTERNAL,
        **extra,
    )


def test_pinned_allocation_of_a_dirty_server_clears_credentials(db_session, clear_calls):
    server = _server(db_session, "alloc-pin", "198.51.100.150")
    bare_metal.create_bare_metal_service(db_session, _request("alloc-pin-svc", server_id=server.id), ACTOR, {})
    db_session.refresh(server)
    assert not server.credentials


def test_group_allocation_clears_old_keys_and_keeps_the_new_install_keys(
    db_session, monkeypatch, tmp_path, clear_calls
):
    from tests.api.test_billing_credentials_isolation import UBUNTU, UBUNTU_PARAMS

    server = _server(db_session, "alloc-grp", "198.51.100.151")
    group = ServerGroupDAO.create(db_session, name="alloc-grp-g")
    group.servers.append(server)
    db_session.commit()
    monkeypatch.setattr(bare_metal, "server_not_ready_reason", lambda *_: None)
    monkeypatch.setattr(bare_metal, "determine_template_for_group", lambda *_: UBUNTU.id)
    bare_metal.create_bare_metal_service(
        db_session,
        _request(
            "alloc-grp-svc",
            server_group_id=group.id,
            template_id=UBUNTU.id,
            template_parameters=dict(UBUNTU_PARAMS),
        ),
        ACTOR,
        {},
    )
    db_session.refresh(server)
    creds = server.credentials
    assert creds["password"] == UBUNTU_PARAMS["password"]
    assert creds["username"] == UBUNTU_PARAMS["username"]
    assert "PREVIOUS-SECRET" not in str(creds)
    assert clear_calls == [server.id]


def test_refused_pin_of_occupied_server_keeps_occupant_keys(db_session, clear_calls):
    server = _server(db_session, "alloc-occ", "198.51.100.154")
    bare_metal.create_bare_metal_service(db_session, _request("alloc-occ-1", server_id=server.id), ACTOR, {})
    server.credentials = dict(STALE)
    db_session.commit()
    with pytest.raises(ProvisioningError):
        bare_metal.create_bare_metal_service(db_session, _request("alloc-occ-2", server_id=server.id), ACTOR, {})
    db_session.expire_all()
    assert db_session.get(type(server), server.id).credentials == STALE


def test_resume_of_the_same_order_keeps_credentials(db_session, monkeypatch, clear_calls):
    server = _server(db_session, "alloc-res", "198.51.100.152")
    group = ServerGroupDAO.create(db_session, name="alloc-res-g")
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="alloc-res-svc",
        server_id=server.id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.PENDING,
        provisioning_source=ProvisioningSource.INTERNAL,
        config={"server_group_id": group.id},
    )
    queued = []
    monkeypatch.setattr(bare_metal, "queue_group_template_install", lambda *a, **k: queued.append(1))
    bare_metal.resume_bare_metal_order(db_session, service, _request("alloc-res-svc"), ACTOR)
    assert queued == [1]
    db_session.refresh(server)
    assert server.credentials == STALE
    assert clear_calls == []


def test_reinstall_of_the_same_service_keeps_its_credentials(db_session, monkeypatch, clear_calls):
    import app.api.billing as billing
    from tests.api.test_billing_credentials_isolation import UBUNTU, UBUNTU_PARAMS, _install_mocks  # noqa: F401

    server = _server(db_session, "alloc-rei", "198.51.100.153")
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="alloc-rei-svc",
        server_id=server.id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
        config={},
    )
    billing._queue_template_install_for_service(db_session, service, UBUNTU.id, dict(UBUNTU_PARAMS))
    db_session.refresh(server)
    assert server.credentials["password"] == UBUNTU_PARAMS["password"]
    assert clear_calls == []
