"""Bare-metal OS templates come from the server group, not catalog OS profiles."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.billing as billing
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.schemas.billing import BillingBareMetalServiceCreate
from app.services.billing_provisioning_service import ProvisioningActor


def _key(db, name="bm-os"):
    integration = BillingIntegrationDAO.create(db, name=name, integration_type="whmcs")
    return integration.plaintext_api_key


def _headers(key):
    return {"Authorization": f"Bearer {key}"}


def _temp_os():
    return SimpleNamespace(
        get_kernel_url=lambda _id, base: f"{base}/vmlinuz",
        get_initrd_url=lambda _id, base: f"{base}/initrd",
        get_kernel_params=lambda _id: "quiet",
        get_squashfs_url=lambda _id, base: f"{base}/squashfs",
    )


def _patch_boot_task_create(monkeypatch):
    original = billing.BootTaskDAO.create

    def create(*args, **kwargs):
        kwargs.pop("status", None)
        return original(*args, **kwargs)

    monkeypatch.setattr(billing.BootTaskDAO, "create", create)


def test_billing_server_groups_resolves_os_templates(client, db_session):
    key = _key(db_session)
    ServerGroupDAO.create(
        db_session,
        name="os-pool",
        description="pool",
        enable_os_templates=True,
        permitted_os_templates=["ubuntu-cloud-image", "missing-on-disk"],
    )
    resp = client.get("/api/billing/server-groups", headers=_headers(key))
    assert resp.status_code == 200, resp.text
    group = next(g for g in resp.json() if g["name"] == "os-pool")
    assert "ubuntu-cloud-image" in group["permitted_os_templates"]
    ids = [t["id"] for t in group["os_templates"]]
    assert "ubuntu-cloud-image" in ids
    assert "missing-on-disk" not in ids
    ubuntu = next(t for t in group["os_templates"] if t["id"] == "ubuntu-cloud-image")
    assert ubuntu["os_type"] == "linux"
    assert "password" in ubuntu["parameters"]
    assert ubuntu["accepts_ssh_key"] is True


def test_billing_os_templates_scoped_to_service_group(client, db_session):
    integration = BillingIntegrationDAO.create(
        db_session, name="bm-os-scope", integration_type="whmcs"
    )
    key = integration.plaintext_api_key
    user = UserDAO.create(
        db_session,
        username="bm-os-user",
        email="bm-os@example.test",
        billing_integration_id=integration.id,
        external_user_id="bm-os",
    )
    loc = LocationDAO.create(db_session, name="bm-os-loc")
    server = ServerDAO.create(
        db_session,
        name="bm-os-srv",
        server_ip="198.51.100.90",
        plugin_name="ipmi",
        plugin_config={},
        location_id=loc.id,
    )
    group = ServerGroupDAO.create(
        db_session,
        name="os-scope-pool",
        enable_os_templates=True,
        permitted_os_templates=["ubuntu-cloud-image"],
    )
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="bm-os-svc",
        server_id=server.id,
        owner_user_id=user.id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.BILLING,
        config={"server_group_id": group.id},
    )
    scoped = client.get(
        "/api/billing/os-templates",
        headers=_headers(key),
        params={"service_id": service.id},
    )
    assert scoped.status_code == 200, scoped.text
    ids = [t["id"] for t in scoped.json()]
    assert ids == ["ubuntu-cloud-image"]

    global_list = client.get("/api/billing/os-templates", headers=_headers(key))
    assert global_list.status_code == 200
    global_ids = [t["id"] for t in global_list.json()]
    assert "ubuntu-cloud-image" not in global_ids


def test_resolve_template_parameters_maps_whmcs_password_and_defaults():
    template = SimpleNamespace(
        id="ubuntu-cloud-image",
        parameters={
            "ubuntu_release": SimpleNamespace(
                type="select", required=True, default="jammy", generate=None
            ),
            "username": SimpleNamespace(
                type="text", required=True, default="rackflow", generate=None
            ),
            "password": SimpleNamespace(
                type="password",
                required=True,
                default=None,
                generate=SimpleNamespace(
                    enabled=True, length=12, charset="alphanumeric", exclude_ambiguous=True
                ),
            ),
            "ssh_public_key": SimpleNamespace(
                type="text", required=False, default="", generate=None
            ),
        },
    )
    resolved = billing.resolve_template_parameters(
        template, {"admin_password": "FromWhmcs!", "ssh_public_keys": ["ssh-ed25519 AAAA"]}
    )
    assert resolved["ubuntu_release"] == "jammy"
    assert resolved["username"] == "rackflow"
    assert resolved["password"] == "FromWhmcs!"
    assert resolved["ssh_public_key"] == "ssh-ed25519 AAAA"


def test_resolve_template_parameters_errors_when_required_unresolved():
    template = SimpleNamespace(
        id="needs-hostname",
        parameters={
            "hostname": SimpleNamespace(type="text", required=True, default=None, generate=None),
        },
    )
    with pytest.raises(HTTPException) as exc:
        billing.resolve_template_parameters(template, {})
    assert exc.value.status_code == 400
    assert "hostname" in str(exc.value.detail)


def test_queue_template_fills_params_without_unsubstituted_placeholders(
    db_session, monkeypatch, tmp_path
):
    integration = BillingIntegrationDAO.create(
        db_session, name="queue-params", integration_type="whmcs"
    )
    user = UserDAO.create(
        db_session,
        username="queue-params-user",
        email="queue-params@example.test",
        billing_integration_id=integration.id,
        external_user_id="queue-params",
    )
    loc = LocationDAO.create(db_session, name="queue-params-loc")
    server = ServerDAO.create(
        db_session,
        name="queue-params-srv",
        server_ip="198.51.100.91",
        plugin_name="ipmi",
        plugin_config={},
        location_id=loc.id,
    )
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="queue-params-svc",
        server_id=server.id,
        owner_user_id=user.id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.PENDING,
        provisioning_source=ProvisioningSource.BILLING,
        config={"server_group_id": 1},
    )
    _patch_boot_task_create(monkeypatch)
    script = tmp_path / "install.sh"
    script.write_text(
        'echo ${PARAM_USERNAME} ${PARAM_PASSWORD} ${PARAM_UBUNTU_RELEASE}',
        encoding="utf-8",
    )
    template = SimpleNamespace(
        id="ubuntu-cloud-image",
        name="Ubuntu",
        os_type="linux",
        disk_image=None,
        template_dir=tmp_path,
        user_reinstallable=False,
        parameters={
            "ubuntu_release": SimpleNamespace(
                type="select", required=True, default="jammy", generate=None
            ),
            "username": SimpleNamespace(
                type="text", required=True, default="rackflow", generate=None
            ),
            "password": SimpleNamespace(
                type="password", required=True, default=None, generate=None
            ),
        },
    )
    monkeypatch.setattr(
        billing,
        "get_template_service",
        lambda: SimpleNamespace(
            get_template=lambda ident: template if ident == "ubuntu-cloud-image" else None,
            get_template_script_path=lambda ident: script,
            enumerate_relative_files=lambda ident: ["install.sh"],
        ),
    )
    monkeypatch.setattr(billing, "get_temp_os_service", _temp_os)
    monkeypatch.setattr(
        billing,
        "get_download_token_service",
        lambda: SimpleNamespace(generate_token=lambda **kwargs: "tok-params"),
    )
    monkeypatch.setattr(billing, "_get_base_url_for_pxe_ip", lambda *_: "https://pxe.test")
    monkeypatch.setattr(billing.InstallationTaskDAO, "get_active_by_server", lambda *_: None)
    monkeypatch.setattr(billing.NetworkPortDAO, "get_pxe_boot_port", lambda *_: None)
    monkeypatch.setattr(billing.DiskDAO, "get_os_disk", lambda *_: None)

    boot_task, _install = billing._queue_template_install_for_service(
        db_session, service, "ubuntu-cloud-image", {"admin_password": "FromWhmcs!"}
    )
    content = boot_task.script_content or ""
    assert "${PARAM_" not in content
    assert "rackflow" in content
    assert "jammy" in content
    assert "FromWhmcs!" in content
    db_session.refresh(service)
    assert service.config["template_id"] == "ubuntu-cloud-image"
    assert service.config["template_parameters"]["password"] == "FromWhmcs!"
    assert service.config["template_parameters"]["username"] == "rackflow"


@pytest.mark.asyncio
async def test_provision_bare_metal_rejects_unpermitted_template(db_session, monkeypatch):
    owner = UserDAO.create(db_session, username="bm-os-owner", email="bm-os-owner@example.test")
    loc = LocationDAO.create(db_session, name="bm-os-rej-loc")
    server = ServerDAO.create(
        db_session,
        name="bm-os-rej-srv",
        server_ip="203.0.113.80",
        plugin_name="ipmi",
        plugin_config={},
        location_id=loc.id,
    )
    group = ServerGroupDAO.create(
        db_session,
        name="bm-os-rej",
        enable_os_templates=True,
        permitted_os_templates=["ubuntu-cloud-image"],
    )
    monkeypatch.setattr(billing, "_select_free_server_in_group", lambda *_: server)
    data = BillingBareMetalServiceCreate(
        name="bm-os-rej-svc",
        external_user_id="e",
        service_config={
            "server_group_id": group.id,
            "template_id": "windows-server-2022",
        },
    )
    actor = ProvisioningActor(kind="integration", actor_id=1, name="t", source="test")
    with pytest.raises(HTTPException) as exc:
        await billing._provision_bare_metal_service(data, owner.id, actor, db_session)
    assert exc.value.status_code == 400
    assert "not permitted" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_provision_bare_metal_queues_permitted_template(db_session, monkeypatch):
    owner = UserDAO.create(db_session, username="bm-os-ok", email="bm-os-ok@example.test")
    loc = LocationDAO.create(db_session, name="bm-os-ok-loc")
    server = ServerDAO.create(
        db_session,
        name="bm-os-ok-srv",
        server_ip="203.0.113.81",
        plugin_name="ipmi",
        plugin_config={},
        location_id=loc.id,
    )
    group = ServerGroupDAO.create(
        db_session,
        name="bm-os-ok",
        enable_os_templates=True,
        permitted_os_templates=["ubuntu-cloud-image"],
    )
    queued = {}

    def capture(**kwargs):
        queued.update(kwargs)
        return SimpleNamespace(id=1), SimpleNamespace(id=2, boot_task_id=1)

    monkeypatch.setattr(billing, "_select_free_server_in_group", lambda *_: server)
    monkeypatch.setattr(billing, "_queue_template_install_for_service", capture)
    data = BillingBareMetalServiceCreate(
        name="bm-os-ok-svc",
        external_user_id="e",
        service_config={
            "server_group_id": group.id,
            "template_id": "ubuntu-cloud-image",
        },
    )
    actor = ProvisioningActor(kind="integration", actor_id=1, name="t", source="test")
    service = await billing._provision_bare_metal_service(data, owner.id, actor, db_session)
    assert service.status == ServiceStatus.PENDING
    assert queued["template_id"] == "ubuntu-cloud-image"
