from app.dao.installation_task_dao import InstallationTaskDAO
from app.dao.service_dao import ServiceDAO
from app.models.boot_task import BootType
from app.models.location import Location
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.installation_task import InstallationStatus


def _create_server(db_session) -> Server:
    location = Location(name="Install DAO Location", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name="install-dao-server",
        server_ip="10.20.30.40",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"host": "10.20.30.40"},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_create_get_update_progress_and_logs(db_session):
    server = _create_server(db_session)

    task = InstallationTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_task_id=123,
        template_id="ubuntu",
        template_parameters={"disk": "/dev/sda"},
        os_name="Ubuntu",
        os_version="22.04",
    )
    assert task.status == InstallationStatus.PENDING
    assert task.template_id == "ubuntu"

    by_id = InstallationTaskDAO.get_by_id(db_session, task.id)
    by_boot = InstallationTaskDAO.get_by_boot_task(db_session, 123)
    assert by_id is not None and by_boot is not None
    assert by_id.id == task.id == by_boot.id

    progress = InstallationTaskDAO.update_progress(db_session, task.id, 120, logs="line-1")
    assert progress.progress_percent == 100
    assert "line-1" in (progress.logs or "")

    progress = InstallationTaskDAO.update_progress(db_session, task.id, -5, logs="line-2")
    assert progress.progress_percent == 0
    assert "line-1" in (progress.logs or "")
    assert "line-2" in (progress.logs or "")

    logs_updated = InstallationTaskDAO.update_logs(db_session, task.id, "replacement-logs")
    assert logs_updated.logs == "replacement-logs"


def test_mark_lifecycle_activate_pending_services_and_cancel(db_session):
    server = _create_server(db_session)
    task = InstallationTaskDAO.create(db_session, server_id=server.id, boot_task_id=456)

    pending_service = ServiceDAO.create_bare_metal(
        db_session,
        name="svc-pending",
        server_id=server.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
    )
    active_service = ServiceDAO.create_bare_metal(
        db_session,
        name="svc-active",
        server_id=server.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
    )

    in_progress = InstallationTaskDAO.mark_in_progress(db_session, task.id)
    assert in_progress.status == InstallationStatus.IN_PROGRESS
    assert in_progress.started_at is not None

    completed = InstallationTaskDAO.mark_completed(db_session, task.id)
    db_session.refresh(pending_service)
    db_session.refresh(active_service)
    assert completed.status == InstallationStatus.COMPLETED
    assert completed.completed_at is not None
    assert completed.progress_percent == 100
    assert pending_service.status == ServiceStatus.ACTIVE
    assert active_service.status == ServiceStatus.ACTIVE

    failed = InstallationTaskDAO.mark_failed(db_session, task.id, error_message="failed-msg")
    assert failed.status == InstallationStatus.FAILED
    assert failed.error_message == "failed-msg"

    second_task = InstallationTaskDAO.create(db_session, server_id=server.id, boot_task_id=789)
    cancelled = InstallationTaskDAO.cancel(db_session, second_task.id)
    assert cancelled.status == InstallationStatus.CANCELLED
    assert cancelled.completed_at is not None


def test_active_by_server_get_by_server_and_delete_pending(db_session):
    server = _create_server(db_session)
    pending = InstallationTaskDAO.create(db_session, server_id=server.id, boot_task_id=1)
    in_progress = InstallationTaskDAO.create(db_session, server_id=server.id, boot_task_id=2)
    InstallationTaskDAO.mark_in_progress(db_session, in_progress.id)
    done = InstallationTaskDAO.create(db_session, server_id=server.id, boot_task_id=3)
    InstallationTaskDAO.mark_completed(db_session, done.id)

    active = InstallationTaskDAO.get_active_by_server(db_session, server.id)
    assert active is not None
    assert active.status in {InstallationStatus.PENDING, InstallationStatus.IN_PROGRESS}

    all_for_server = InstallationTaskDAO.get_by_server(db_session, server.id)
    assert len(all_for_server) == 3

    deleted = InstallationTaskDAO.delete_pending_by_server(db_session, server.id)
    assert deleted == 1
    assert InstallationTaskDAO.get_by_id(db_session, pending.id) is None
    assert InstallationTaskDAO.get_by_id(db_session, in_progress.id) is not None
    assert InstallationTaskDAO.get_by_id(db_session, done.id) is not None

