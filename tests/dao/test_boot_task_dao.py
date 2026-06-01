from app.dao.boot_task_dao import BootTaskDAO
from app.models.boot_task import BootTaskStatus, BootType
from app.models.location import Location
from app.models.server import Server


def _create_server(db_session) -> Server:
    location = Location(name="Boot DAO Location", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name="boot-dao-server",
        server_ip="10.10.10.10",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"host": "10.10.10.10"},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_create_cancels_previous_pending_and_fallback_boot_type(db_session):
    server = _create_server(db_session)

    first = BootTaskDAO.create(db_session, server_id=server.id, boot_type=BootType.LINUX_SCRIPT.value)
    assert first.status == BootTaskStatus.PENDING

    second = BootTaskDAO.create(db_session, server_id=server.id, boot_type="invalid_type")
    db_session.refresh(first)

    assert second.boot_type == BootType.LINUX_SCRIPT
    assert first.status == BootTaskStatus.CANCELLED
    assert BootTaskDAO.get_pending_by_server(db_session, server.id).id == second.id


def test_mark_lifecycle_and_queries(db_session):
    server = _create_server(db_session)
    task = BootTaskDAO.create(db_session, server_id=server.id, boot_type=BootType.ISO.value)

    in_progress = BootTaskDAO.mark_in_progress(db_session, task.id)
    assert in_progress.status == BootTaskStatus.IN_PROGRESS
    assert in_progress.started_at is not None

    active = BootTaskDAO.get_active_by_server(db_session, server.id)
    assert active is not None
    assert active.id == task.id

    completed = BootTaskDAO.mark_completed(db_session, task.id)
    assert completed.status == BootTaskStatus.COMPLETED
    assert completed.completed_at is not None

    failed = BootTaskDAO.mark_failed(db_session, task.id, error_message="boom")
    assert failed.status == BootTaskStatus.FAILED
    assert failed.error_message == "boom"

    all_tasks = BootTaskDAO.get_all_by_server(db_session, server.id)
    assert len(all_tasks) == 1
    assert all_tasks[0].id == task.id

    assert BootTaskDAO.delete(db_session, task.id) is True
    assert BootTaskDAO.get_by_id(db_session, task.id) is None
    assert BootTaskDAO.delete(db_session, 99999) is False

