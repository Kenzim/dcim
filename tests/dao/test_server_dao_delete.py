from app.dao.boot_task_dao import BootTaskDAO
from app.dao.server_dao import ServerDAO
from app.models.boot_task import BootType
from app.models.location import Location
from app.models.server import Server
from app.models.server_activity import (
    ServerActivity,
    ServerActivityEventType,
    ServerActivityStatus,
)


def _create_server(db_session, name: str = "delete-dao-server") -> Server:
    location = Location(name=f"Loc for {name}", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name=name,
        server_ip="10.20.30.40",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"host": "10.20.30.40"},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_delete_server_with_boot_tasks_and_activity(db_session):
    server = _create_server(db_session)
    BootTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_type=BootType.LINUX_SCRIPT.value,
        description="pending boot",
    )
    db_session.add(
        ServerActivity(
            server_id=server.id,
            event_type=ServerActivityEventType.POWER,
            action="power_on",
            status=ServerActivityStatus.SUCCESS,
            message="powered on",
            source="test",
        )
    )
    db_session.commit()

    server_id = server.id
    assert ServerDAO.delete(db_session, server_id) is True
    db_session.expire_all()
    assert ServerDAO.get_by_id(db_session, server_id) is None
    assert BootTaskDAO.get_all_by_server(db_session, server_id) == []


def test_delete_server_missing_returns_false(db_session):
    assert ServerDAO.delete(db_session, 999999) is False
