from app.dao.boot_task_dao import BootTaskDAO
from app.dao.installation_task_dao import InstallationTaskDAO
from app.dao.network_port_dao import NetworkPortDAO
from app.models.location import Location
from app.models.server import Server
from app.models.boot_task import BootType


def _create_server(db_session, *, name: str, server_ip: str) -> Server:
    location = Location(name=f"{name}-location", description="test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name=name,
        server_ip=server_ip,
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"hostname": server_ip, "username": "admin", "password": "pass"},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_cloud_init_endpoints_resolve_by_pxe_ip_and_render_payload(client, db_session):
    server = _create_server(db_session, name="cloud-init-pxe", server_ip="192.0.2.10")

    NetworkPortDAO.create(
        db_session,
        server_id=server.id,
        name="eth0",
        speed_mbps=1000,
        mac_address="00:11:22:33:44:55",
        pxe_boot=True,
        pxe_ip="10.20.30.40",
    )

    boot_task = BootTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live",
        description="ubuntu cloud image install",
    )
    InstallationTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_task_id=boot_task.id,
        template_id="ubuntu-cloud-image",
        template_parameters={
            "username": "clouduser",
            "password": "Passw0rd!123",
            "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB test@example",
        },
        os_name="Ubuntu Cloud Image",
    )

    headers = {"X-Forwarded-For": "10.20.30.40"}
    user_data_resp = client.get("/api/servers/interaction/cloud-init/user-data", headers=headers)
    assert user_data_resp.status_code == 200
    assert "#cloud-config" in user_data_resp.text
    assert "name: 'clouduser'" in user_data_resp.text
    assert "password: 'Passw0rd!123'" in user_data_resp.text
    assert "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB test@example" in user_data_resp.text

    meta_data_resp = client.get("/api/servers/interaction/cloud-init/meta-data", headers=headers)
    assert meta_data_resp.status_code == 200
    assert "instance-id: rackflow-" in meta_data_resp.text
    assert "local-hostname: cloud-init-pxe" in meta_data_resp.text


def test_cloud_init_endpoints_fallback_to_server_ip(client, db_session):
    server = _create_server(db_session, name="cloud-init-server-ip", server_ip="198.51.100.20")
    boot_task = BootTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live",
        description="fallback context",
    )
    InstallationTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_task_id=boot_task.id,
        template_id="ubuntu-cloud-image",
        template_parameters={
            "username": "fallbackuser",
            "password": "TempPass123!",
        },
        os_name="Ubuntu Cloud Image",
    )

    headers = {"X-Forwarded-For": "198.51.100.20"}
    response = client.get("/api/servers/interaction/cloud-init/user-data", headers=headers)
    assert response.status_code == 200
    assert "name: 'fallbackuser'" in response.text


def test_cloud_init_user_data_returns_404_without_install_context(client, db_session):
    server = _create_server(db_session, name="cloud-init-no-context", server_ip="203.0.113.30")
    NetworkPortDAO.create(
        db_session,
        server_id=server.id,
        name="eth0",
        speed_mbps=1000,
        mac_address="00:AA:BB:CC:DD:EE",
        pxe_boot=True,
        pxe_ip="203.0.113.31",
    )

    headers = {"X-Forwarded-For": "203.0.113.31"}
    response = client.get("/api/servers/interaction/cloud-init/user-data", headers=headers)
    assert response.status_code == 404
    assert "No installation context found" in response.json()["detail"]
