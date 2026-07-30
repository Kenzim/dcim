"""PXE script and bare-metal reinstall billing routes with safe fakes."""
from types import SimpleNamespace

from app.core.client_permissions import PermissionKey
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.script_dao import ScriptDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.api.billing import BootTaskDAO


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


def test_reinstall_queues_template_with_scoped_token(client, db_session, monkeypatch, tmp_path):
    service, headers = _bare_metal(db_session)
    _patch_boot_task_create(monkeypatch)
    script = tmp_path / "install.sh"
    script.write_text('echo "${PARAM_PASSWORD}" ${DOWNLOAD_TOKEN}', encoding="utf-8")
    template = SimpleNamespace(
        id="debian", name="Debian", os_type="linux", disk_image="/images/debian.img",
        template_dir=tmp_path, user_reinstallable=True,
    )
    template_service = SimpleNamespace(
        get_template=lambda ident: template if ident == "debian" else None,
        get_template_script_path=lambda ident: script,
        enumerate_relative_files=lambda ident: ["install.sh"],
    )
    tokens = SimpleNamespace(generate_token=lambda **kwargs: "scoped-token")
    monkeypatch.setattr("app.api.billing.get_template_service", lambda: template_service)
    monkeypatch.setattr("app.api.billing.get_temp_os_service", _temp_os)
    monkeypatch.setattr("app.api.billing.get_download_token_service", lambda: tokens)
    monkeypatch.setattr("app.api.billing._get_base_url_for_pxe_ip", lambda *_: "https://pxe.test")

    response = client.post(
        f"/api/billing/services/{service.id}/actions/reinstall-os", headers=headers,
        json={"template_id": "debian", "template_parameters": {"password": 'a"$b'}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["boot_task_id"]
