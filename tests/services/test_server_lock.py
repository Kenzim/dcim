"""A locked server refuses every OS install / wipe path."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.dao import BootTaskDAO, InstallationTaskDAO, NetworkPortDAO
from app.models.boot_task import BootTask, BootTaskStatus, BootType
from app.models.location import Location
from app.models.server import Server
from app.services.server_lock import ServerLockedError


@pytest.fixture
def locked_server(db_session):
    loc = Location(name="lock-loc", description="")
    db_session.add(loc)
    db_session.flush()
    server = Server(
        name="lock-srv",
        server_ip="10.9.9.9",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
        locked=True,
        lock_reason="customer data",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


@pytest.fixture
def admin_client(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    return client


def test_unlocked_server_still_accepts_boot_tasks(db_session, locked_server):
    locked_server.locked = False
    db_session.commit()
    task = BootTaskDAO.create(db_session, server_id=locked_server.id, boot_type="linux_script")
    assert task.id


def test_boot_task_dao_refuses_locked_server(db_session, locked_server):
    with pytest.raises(ServerLockedError) as exc:
        BootTaskDAO.create(db_session, server_id=locked_server.id, boot_type="linux_script")
    assert exc.value.status_code == 409
    assert exc.value.detail == "Server is locked: customer data"


def test_installation_task_dao_refuses_locked_server(db_session, locked_server):
    with pytest.raises(ServerLockedError):
        InstallationTaskDAO.create(db_session, server_id=locked_server.id, boot_task_id=1, template_id="x")


def test_queue_template_install_for_service_refuses(db_session, locked_server, monkeypatch):
    from app.api import billing

    monkeypatch.setattr(billing, "service_linked_server", lambda db, service: locked_server)
    with pytest.raises(HTTPException) as exc:
        billing._queue_template_install_for_service(db_session, object(), "debian-12", None)
    assert exc.value.status_code == 409
    assert "Server is locked: customer data" in exc.value.detail
    assert db_session.query(BootTask).count() == 0


def test_locked_server_not_ready_for_provisioning(db_session, locked_server):
    from app.services.provisioning.bare_metal import _pin_existing_server, server_not_ready_reason
    from app.services.provisioning.errors import ProvisioningError

    assert server_not_ready_reason(db_session, locked_server) == "locked"
    with pytest.raises(ProvisioningError) as exc:
        _pin_existing_server(db_session, locked_server.id)
    assert "Server is locked: customer data" in str(exc.value)


@pytest.mark.asyncio
async def test_launch_boot_task_cancels_for_locked_server(db_session, locked_server):
    from app.services.boot_launcher import launch_boot_task

    task = BootTask(server_id=locked_server.id, boot_type=BootType.TEMP_OS, status=BootTaskStatus.PENDING)
    db_session.add(task)
    db_session.commit()
    assert await launch_boot_task(db_session, locked_server, task, source="test") == "blocked"
    db_session.refresh(task)
    assert task.status == BootTaskStatus.CANCELLED


def test_pxe_does_not_serve_queued_task_for_locked_server(client, db_session, locked_server):
    NetworkPortDAO.create(
        db_session,
        server_id=locked_server.id,
        name="eth0",
        mac_address="00:0e:1e:6f:16:b1",
        speed_mbps=1000,
        pxe_boot=True,
        pxe_ip="10.9.9.9",
    )
    task = BootTask(
        server_id=locked_server.id,
        boot_type=BootType.TEMP_OS,
        status=BootTaskStatus.PENDING,
        script_content="#!/bin/bash\nexit 0\n",
    )
    db_session.add(task)
    db_session.commit()
    response = client.get("/api/servers/interaction/pxe", params={"mac": "00:0e:1e:6f:16:b1"})
    assert response.status_code == 200
    assert "local disk" in response.text
    db_session.refresh(task)
    assert task.status == BootTaskStatus.CANCELLED


def test_admin_boot_task_and_hardware_detection_refused(admin_client, locked_server):
    response = admin_client.post(
        f"/api/servers/interaction/{locked_server.id}/boot-task",
        json={"boot_type": "temp_os", "temp_os_id": "alpine"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Server is locked: customer data"

    response = admin_client.post(f"/api/servers/{locked_server.id}/hardware-detection/run")
    assert response.status_code == 409


def test_admin_update_toggles_lock_and_logs(admin_client, db_session, locked_server):
    from app.models.server_activity import ServerActivity

    response = admin_client.put(f"/api/servers/{locked_server.id}", json={"locked": False})
    assert response.status_code == 200
    body = response.json()
    assert body["locked"] is False and body["lock_reason"] is None

    response = admin_client.put(
        f"/api/servers/{locked_server.id}", json={"locked": True, "lock_reason": "  keep  "}
    )
    assert response.status_code == 200
    assert response.json()["locked"] is True and response.json()["lock_reason"] == "keep"
    actions = [a.action for a in db_session.query(ServerActivity).filter_by(server_id=locked_server.id)]
    assert "unlock" in actions and "lock" in actions
