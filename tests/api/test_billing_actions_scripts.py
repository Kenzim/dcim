"""PXE script and bare-metal reinstall billing routes with safe fakes."""
from types import SimpleNamespace

from app.core.client_permissions import PermissionKey
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.script_dao import ScriptDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.boot_task import BootTaskStatus
from app.models.installation_task import InstallationStatus
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.api.billing import BootTaskDAO, InstallationTaskDAO


def _bare_metal(db):
    integration = BillingIntegrationDAO.create(db, name="actions", integration_type="whmcs")
    user = UserDAO.create(
        db, username="actions-user", email="actions@example.test",
        billing_integration_id=integration.id, external_user_id="actions-user",
    )
    loc = LocationDAO.create(db, name="actions-loc")
    server = ServerDAO.create(
        db, name="actions-server", server_ip="198.51.100.80", plugin_name="ipmi",
        plugin_config={}, location_id=loc.id,
    )
    service = ServiceDAO.create_bare_metal(
        db, name="actions-service", server_id=server.id, owner_user_id=user.id,
        service_type=ServiceType.BARE_METAL, status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.BILLING,
    )
    service.permission_overrides = {
        PermissionKey.BMS_RUN_SCRIPT: True, PermissionKey.BMS_REINSTALL: True,
    }
    ServiceDAO.update(db, service)
    return service, {"Authorization": f"Bearer {integration.plaintext_api_key}"}


def _temp_os():
    return SimpleNamespace(
        get_os_config=lambda _id: {"id": "debian-live"},
        get_kernel_url=lambda _id, base: f"{base}/vmlinuz",
        get_initrd_url=lambda _id, base: f"{base}/initrd",
        get_kernel_params=lambda _id: "quiet",
        get_squashfs_url=lambda _id, base: f"{base}/squashfs",
    )


def _patch_boot_task_create(monkeypatch):
    """The route passes a model status unsupported by the historical DAO API."""
    original = BootTaskDAO.create

    def create(*args, **kwargs):
        kwargs.pop("status", None)
        return original(*args, **kwargs)

    monkeypatch.setattr(BootTaskDAO, "create", create)


def test_run_script_queues_templated_boot_task(client, db_session, monkeypatch):
    service, headers = _bare_metal(db_session)
    _patch_boot_task_create(monkeypatch)
    script = ScriptDAO.create(
        db_session, name="billing-script", content="echo ${SERVER_IP} $CUSTOM",
        user_executable=True,
    )
    monkeypatch.setattr("app.api.billing.get_temp_os_service", _temp_os)
    monkeypatch.setattr("app.api.billing._get_base_url_for_pxe_ip", lambda *_: "https://pxe.test")
    response = client.post(
        f"/api/billing/services/{service.id}/actions/run-script", headers=headers,
        json={"script_id": script.id, "parameters": {"custom": "done"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["boot_task_id"]


def _reinstall_fakes(monkeypatch, tmp_path, *, user_reinstallable=True, os_type="linux"):
    """Fake template/temp-OS/token services; returns the recorded launch and release calls."""
    script = tmp_path / "install.sh"
    script.write_text(
        'P="${PARAM_PASSWORD}" T=${DOWNLOAD_TOKEN} INSTALLATION_TASK_ID="${INSTALLATION_TASK_ID}"',
        encoding="utf-8",
    )
    template = SimpleNamespace(
        id="debian", name="Debian", os_type=os_type, disk_image="/images/debian.img",
        template_dir=tmp_path, user_reinstallable=user_reinstallable,
    )
    template_service = SimpleNamespace(
        get_template=lambda ident: template if ident == "debian" else None,
        get_template_script_path=lambda ident: script,
        enumerate_relative_files=lambda ident: ["install.sh"],
    )
    tokens = SimpleNamespace(generate_token=lambda **kwargs: "scoped-token")
    calls = {"launch": [], "release": []}

    async def release(_db, server):
        calls["release"].append(server.id)

    monkeypatch.setattr("app.api.billing.get_template_service", lambda: template_service)
    monkeypatch.setattr("app.api.billing.get_temp_os_service", _temp_os)
    monkeypatch.setattr("app.api.billing.get_download_token_service", lambda: tokens)
    monkeypatch.setattr("app.api.billing._get_base_url_for_pxe_ip", lambda *_: "https://pxe.test")
    monkeypatch.setattr(
        "app.api.billing.schedule_launch",
        lambda server_id, boot_task_id, **kw: calls["launch"].append((server_id, boot_task_id, kw)),
    )
    monkeypatch.setattr("app.api.billing.release_boot_media", release)
    return calls


def test_reinstall_queues_template_with_scoped_token(client, db_session, monkeypatch, tmp_path):
    service, headers = _bare_metal(db_session)
    _reinstall_fakes(monkeypatch, tmp_path)

    response = client.post(
        f"/api/billing/services/{service.id}/actions/reinstall-os", headers=headers,
        json={"template_id": "debian", "template_parameters": {"password": 'a"$b'}},
    )
    assert response.status_code == 200, response.text
    boot_task = BootTaskDAO.get_by_id(db_session, response.json()["boot_task_id"])
    assert "scoped-token" in boot_task.script_content
    assert 'P="a\\"\\$b"' in boot_task.script_content


def test_reinstall_creates_installation_task_and_launches(client, db_session, monkeypatch, tmp_path):
    service, headers = _bare_metal(db_session)
    calls = _reinstall_fakes(monkeypatch, tmp_path)

    response = client.post(
        f"/api/billing/services/{service.id}/actions/reinstall-os", headers=headers,
        json={"template_id": "debian", "template_parameters": {"admin_password": "s3cret", "username": "rackflow"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    server_id = service.bare_metal.server_id

    install = InstallationTaskDAO.get_active_by_server(db_session, server_id)
    assert install is not None
    assert body["installation_task_id"] == install.id
    assert install.boot_task_id == body["boot_task_id"]
    assert install.template_id == "debian"
    boot_task = BootTaskDAO.get_by_id(db_session, body["boot_task_id"])
    assert f'INSTALLATION_TASK_ID="{install.id}"' in boot_task.script_content
    assert calls["launch"] == [(server_id, boot_task.id, {"source": "billing_api", "service_id": service.id})]
    assert calls["release"] == [server_id]

    # Login fields for the billing module; the password is not echoed back.
    assert body["server_ip"] == "198.51.100.80"
    assert body["credentials"] == {"os_type": "linux"}
    assert body["config"]["template_parameters"] == {"username": "rackflow"}
    db_session.refresh(service)
    assert service.status == ServiceStatus.ACTIVE
    assert service.config["template_parameters"]["admin_password"] == "s3cret"


def test_reinstall_cancels_open_tasks(client, db_session, monkeypatch, tmp_path):
    service, headers = _bare_metal(db_session)
    calls = _reinstall_fakes(monkeypatch, tmp_path)
    server_id = service.bare_metal.server_id
    stuck_boot = BootTaskDAO.create(db_session, server_id=server_id, boot_type="temp_os")
    stuck_boot.status = BootTaskStatus.IN_PROGRESS
    db_session.commit()
    stuck_install = InstallationTaskDAO.create(
        db_session, server_id=server_id, boot_task_id=stuck_boot.id, template_id="debian",
        template_parameters={}, os_name="Debian",
    )
    InstallationTaskDAO.mark_in_progress(db_session, stuck_install.id)

    response = client.post(
        f"/api/billing/services/{service.id}/actions/reinstall-os", headers=headers,
        json={"template_id": "debian"},
    )
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert BootTaskDAO.get_by_id(db_session, stuck_boot.id).status == BootTaskStatus.CANCELLED
    assert InstallationTaskDAO.get_by_id(db_session, stuck_install.id).status == InstallationStatus.CANCELLED
    new_install = InstallationTaskDAO.get_active_by_server(db_session, server_id)
    assert new_install.id == response.json()["installation_task_id"]
    assert BootTaskDAO.get_active_by_server(db_session, server_id).id == response.json()["boot_task_id"]
    assert len(calls["launch"]) == 1


def test_reinstall_rejects_template_not_allowed(client, db_session, monkeypatch, tmp_path):
    service, headers = _bare_metal(db_session)
    calls = _reinstall_fakes(monkeypatch, tmp_path, user_reinstallable=False)
    server_id = service.bare_metal.server_id
    open_boot = BootTaskDAO.create(db_session, server_id=server_id, boot_type="temp_os")

    response = client.post(
        f"/api/billing/services/{service.id}/actions/reinstall-os", headers=headers,
        json={"template_id": "debian"},
    )
    assert response.status_code == 403, response.text
    db_session.expire_all()
    assert BootTaskDAO.get_by_id(db_session, open_boot.id).status == BootTaskStatus.PENDING
    assert InstallationTaskDAO.get_active_by_server(db_session, server_id) is None
    assert calls == {"launch": [], "release": []}
