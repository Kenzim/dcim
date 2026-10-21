"""Download tokens baked into a queued install script start their lifetime when
the server fetches the script, not when the task is queued (a server can take
much longer than the token TTL to boot)."""
import pytest

from app.dao import BootTaskDAO, NetworkPortDAO
from app.models.location import Location
from app.models.server import Server
from app.services.download_token_service import get_download_token_service, template_file_scope

TEMPLATE_ID = "windows-server-2022"
MAC = "00:0e:1e:6f:16:c1"


@pytest.fixture
def boot_task(db_session):
    location = Location(name="Deferred Token Location", description="Test")
    db_session.add(location)
    db_session.commit()
    server = Server(
        name="deferred-token-server",
        server_ip="192.168.1.120",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"hostname": "192.168.1.120"},
    )
    db_session.add(server)
    db_session.commit()
    NetworkPortDAO.create(
        db_session,
        server_id=server.id,
        name="eth0",
        mac_address=MAC,
        speed_mbps=1000,
        pxe_boot=True,
        pxe_ip="192.168.1.120",
    )
    return BootTaskDAO.create(
        db=db_session,
        server_id=server.id,
        boot_type="temp_os",
        script_content="#!/bin/bash\necho install\n",
        description="Deferred token test",
    )


def _tokens(boot_task_id):
    svc = get_download_token_service()
    image_token = svc.generate_token(
        boot_task_id=boot_task_id,
        allowed_files=[template_file_scope(TEMPLATE_ID, "firstboot.ps1")],
        expires_in=3600,
        deferred=True,
    )
    script_token = svc.generate_token(
        boot_task_id=boot_task_id,
        allowed_files=[f"script-{boot_task_id}.sh"],
        single_use=False,
    )
    return image_token, script_token


def _template_file(client, token):
    return client.get(
        f"/api/servers/interaction/template-files/{TEMPLATE_ID}/firstboot.ps1",
        params={"token": token},
    )


def test_scripts_endpoint_activates_template_token(client, boot_task):
    image_token, script_token = _tokens(boot_task.id)

    assert _template_file(client, image_token).status_code == 401

    resp = client.get(f"/api/servers/interaction/scripts/{boot_task.id}", params={"token": script_token})
    assert resp.status_code == 200

    assert _template_file(client, image_token).status_code == 200


def test_pxe_script_request_activates_template_token(client, boot_task):
    image_token, script_token = _tokens(boot_task.id)

    assert _template_file(client, image_token).status_code == 401

    resp = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": MAC, "script": "true", "token": script_token},
    )
    assert resp.status_code == 200

    assert _template_file(client, image_token).status_code == 200
