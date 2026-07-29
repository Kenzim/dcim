"""Admin API: deployment-jobs list/detail + provision-vm enqueues a job."""
from unittest.mock import patch

from app.dao.service_dao import ServiceDAO
from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.models.product_catalog import VMTemplate
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import ProvisioningSource, ServiceStatus


def _login_admin(client, test_admin_user):
    r = client.post(
        "/api/users/login",
        json={"username": "admin", "password": "adminpassword123"},
    )
    assert r.status_code == 200


def _vm_service_with_job(db_session):
    cluster = ProxmoxCluster(
        name="c1",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
        enabled=True,
    )
    db_session.add(cluster)
    db_session.commit()
    db_session.refresh(cluster)
    tmpl = VMTemplate(
        code="ubuntu-2204",
        name="U",
        os_type="Linux - Cloudinit",
        proxmox_template_name="ubuntu-2204",
    )
    db_session.add(tmpl)
    db_session.commit()
    db_session.refresh(tmpl)
    service = ServiceDAO.create_vm(
        db_session,
        name="vm-api-1",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
        vm_template_id=tmpl.id,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve1",
        proxmox_vmid=5001,
    )
    return service


def test_list_and_get_deployment_jobs(client, db_session, test_admin_user):
    _login_admin(client, test_admin_user)
    service = _vm_service_with_job(db_session)
    job = VMDeploymentJobDAO.create_job(
        db_session,
        service_id=service.id,
        strategy_name="cloudinit_clone",
        step_names=["clone_from_template", "configure_sizing", "power_on"],
    )

    r = client.get(f"/api/admin/services/{service.id}/deployment-jobs")
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1
    assert jobs[0]["id"] == job.id
    assert jobs[0]["strategy_name"] == "cloudinit_clone"
    assert [s["name"] for s in jobs[0]["steps"]] == [
        "clone_from_template",
        "configure_sizing",
        "power_on",
    ]

    r2 = client.get(f"/api/admin/services/{service.id}/deployment-jobs/{job.id}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "queued"

    # Wrong service id -> 404
    assert client.get(f"/api/admin/services/{service.id}/deployment-jobs/999999").status_code == 404


def test_provision_vm_enqueues_job(client, db_session, test_admin_user):
    _login_admin(client, test_admin_user)
    service = _vm_service_with_job(db_session)

    # Placement already set; enqueue should create a queued job and return the service.
    with patch(
        "app.services.vm_strategy_executor.reserve_vmid_for_service", return_value=5001
    ):
        r = client.post(f"/api/admin/services/{service.id}/provision-vm")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vm_guest_state"] == "provisioning"

    jobs = VMDeploymentJobDAO.list_by_service(db_session, service.id)
    assert len(jobs) == 1
    assert jobs[0].strategy_name == "cloudinit_clone"
    assert jobs[0].status.value == "queued"
