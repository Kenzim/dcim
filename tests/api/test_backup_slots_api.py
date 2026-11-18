"""Backup slots over HTTP: client portal, admin and WHMCS billing surfaces, with Proxmox faked."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.client_permissions import PermissionKey
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.service_dao import ServiceDAO
from app.models.backup_slot import BackupSlot
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from tests.services.test_backup_slots import ACTOR, GIB, SPECS, STORAGE, FakePbs  # noqa: F401
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.product_catalog import VMTemplate
from app.models.user import User
from app.services.provisioning import ProvisioningService, ProvisionRequest


def _login(client, username, password="secret123"):
    resp = client.post("/api/users/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def env(db_session):
    cluster = ProxmoxCluster(
        name="slots-api", api_url="https://pve.example:8006", username="root@pam", password="x",
        verify_ssl=False, vmid_min=5000, vmid_max=6000,
    )
    db_session.add(cluster)
    users = {}
    for name in ("alice", "bob"):
        user = User(username=name, email=f"{name}@example.com", is_admin=False)
        user.set_password("secret123")
        db_session.add(user)
        users[name] = user
    db_session.add(VMTemplate(code="debian-13", name="Debian 13", os_type="Linux - Cloudinit", proxmox_template_name="d13"))
    db_session.commit()

    def vm(owner, name, vmid):
        service = ServiceDAO.create_vm(
            db_session, name=name, owner_user_id=owner.id, provisioning_source=ProvisioningSource.INTERNAL,
            status=ServiceStatus.ACTIVE, proxmox_cluster_id=cluster.id, proxmox_node_name="pve", proxmox_vmid=vmid,
            product_code="p", product_snapshot={"effective_specs": dict(SPECS)},
            config={"vm_plan": {"strategy_name": "cloudinit_clone", "effective_specs": dict(SPECS)}},
        )
        service.permission_overrides = {PermissionKey.VM_BACKUPS: True}
        ServiceDAO.update(db_session, service)
        return service

    def slot(owner, size_gb=50):
        return ProvisioningService.create(
            db_session,
            ProvisionRequest(name=f"slot-{owner.username}-{size_gb}", service_type=ServiceType.BACKUP_SLOT, owner_user_id=owner.id,
                             provisioning_source=ProvisioningSource.INTERNAL, size_gb=size_gb),
            ACTOR,
        )

    pbs = FakePbs()
    e = type("Env", (), {})()
    e.users, e.pbs, e.cluster, e.vm, e.slot = users, pbs, cluster, vm, slot
    e.alice_vm1, e.alice_vm2 = vm(users["alice"], "a1", 5100), vm(users["alice"], "a2", 5101)
    e.bob_vm = vm(users["bob"], "b1", 5200)
    e.alice_slot, e.bob_slot = slot(users["alice"]), slot(users["bob"])

    async def plugin_for(db, service):
        return pbs, cluster.id, "pve", service.vm.proxmox_vmid

    patches = [
        patch("app.services.vm_backup_service.get_vm_backup_plugin", new=AsyncMock(side_effect=plugin_for)),
        patch("app.services.vm_backup_service.list_running_backup_jobs", new=AsyncMock(return_value=[])),
        patch("app.services.backup_slot_service.get_cluster_plugin", new=lambda *a, **k: pbs),
    ]
    for p in patches:
        p.start()
    yield e
    for p in patches:
        p.stop()


def test_client_retain_list_and_release(client, env):
    headers = _login(client, "alice")
    volid = env.pbs.add(5100, "a")
    empty = client.get("/api/client/backup-slots", headers=headers)
    assert empty.status_code == 200
    assert [s["service_id"] for s in empty.json()["slots"]] == [env.alice_slot.id]
    assert empty.json()["slots"][0]["occupied"] is False

    kept = client.post(
        f"/api/client/backup-slots/{env.alice_slot.id}/retain",
        headers=headers,
        json={"source_service_id": env.alice_vm1.id, "volid": volid, "name": "golden"},
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["retained"]["name"] == "golden"
    assert (STORAGE, volid, True) in env.pbs.protect_calls
    targets = {t["service_id"] for t in kept.json()["slot"]["restore_targets"]}
    assert targets == {env.alice_vm1.id, env.alice_vm2.id}

    backups = client.get(f"/api/services/{env.alice_vm1.id}/vm/backups", headers=headers).json()["backups"]
    assert backups[0]["retained"] is True

    freed = client.post(f"/api/client/backup-slots/{env.alice_slot.id}/release", headers=headers)
    assert freed.status_code == 200 and freed.json()["released"] is True
    assert volid in env.pbs.deleted


def test_client_cannot_touch_another_clients_slot_or_vm(client, env):
    bob = _login(client, "bob")
    alice = _login(client, "alice")
    volid = env.pbs.add(5100, "a")
    # Bob cannot see or use Alice's slot.
    assert client.get(f"/api/client/backup-slots/{env.alice_slot.id}", headers=bob).status_code == 404
    assert client.post(f"/api/client/backup-slots/{env.alice_slot.id}/release", headers=bob).status_code == 404
    # Bob cannot retain Alice's VM backup into his own slot.
    stolen = client.post(
        f"/api/client/backup-slots/{env.bob_slot.id}/retain",
        headers=bob,
        json={"source_service_id": env.alice_vm1.id, "volid": volid},
    )
    assert stolen.status_code == 404
    # Alice cannot put her backup into Bob's slot or restore onto Bob's VM.
    into_bobs = client.post(
        f"/api/client/backup-slots/{env.bob_slot.id}/retain",
        headers=alice,
        json={"source_service_id": env.alice_vm1.id, "volid": volid},
    )
    assert into_bobs.status_code == 404
    client.post(
        f"/api/client/backup-slots/{env.alice_slot.id}/retain",
        headers=alice,
        json={"source_service_id": env.alice_vm1.id, "volid": volid},
    )
    onto_bobs = client.post(
        f"/api/client/backup-slots/{env.alice_slot.id}/restore",
        headers=alice,
        json={"target_service_id": env.bob_vm.id, "confirm_data_loss": True},
    )
    assert onto_bobs.status_code == 404
    # Bob restoring the retained volid onto his own VM through the VM route is refused too.
    sneaky = client.post(
        f"/api/services/{env.bob_vm.id}/vm/backups/restore",
        headers=bob,
        json={"volid": volid, "storage": STORAGE, "confirm_data_loss": True},
    )
    assert sneaky.status_code == 403, sneaky.text
    assert env.pbs.restore_calls == []


def test_client_restore_needs_confirmation_then_queues(client, env):
    alice = _login(client, "alice")
    volid = env.pbs.add(5100, "a")
    client.post(
        f"/api/client/backup-slots/{env.alice_slot.id}/retain",
        headers=alice,
        json={"source_service_id": env.alice_vm1.id, "volid": volid},
    )
    refused = client.post(
        f"/api/client/backup-slots/{env.alice_slot.id}/restore", headers=alice, json={"target_service_id": env.alice_vm2.id}
    )
    assert refused.status_code == 409 and "replaces everything" in refused.json()["detail"]
    queued = client.post(
        f"/api/client/backup-slots/{env.alice_slot.id}/restore",
        headers=alice,
        json={"target_service_id": env.alice_vm2.id, "confirm_data_loss": True},
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued" and queued.json()["target_service_id"] == env.alice_vm2.id

    # The same restore through the VM backups route.
    again = client.post(
        f"/api/services/{env.alice_vm2.id}/vm/backups/restore",
        headers=alice,
        json={"volid": volid, "storage": STORAGE},
    )
    assert again.status_code == 409


def test_client_without_permission_is_refused(client, db_session, env):
    env.alice_slot.permission_overrides = {PermissionKey.BACKUP_SLOT_MANAGE: False}
    ServiceDAO.update(db_session, env.alice_slot)
    alice = _login(client, "alice")
    resp = client.post(f"/api/client/backup-slots/{env.alice_slot.id}/release", headers=alice)
    assert resp.status_code == 403
    assert client.get(f"/api/client/backup-slots/{env.alice_slot.id}", headers=alice).status_code == 200


def test_client_lists_slots_without_any_vm(client, db_session, env):
    carol = User(username="carol", email="carol@example.com", is_admin=False)
    carol.set_password("secret123")
    db_session.add(carol)
    db_session.commit()
    slot = env.slot(carol, size_gb=10)
    resp = client.get("/api/client/backup-slots", headers=_login(client, "carol"))
    assert resp.status_code == 200
    assert resp.json()["slots"][0]["service_id"] == slot.id and resp.json()["slots"][0]["size_gb"] == 10


def test_staff_cannot_use_the_client_endpoints(client, test_admin_user):
    headers = _login(client, "admin", "adminpassword123")
    assert client.get("/api/client/backup-slots", headers=headers).status_code == 403


def test_admin_create_resize_release(client, db_session, env, test_admin_user):
    admin = _login(client, "admin", "adminpassword123")
    created = client.post(
        "/api/admin/backup-slots",
        headers=admin,
        json={"owner_user_id": env.users["alice"].id, "name": "staff slot", "size_gb": 20},
    )
    assert created.status_code == 201, created.text
    sid = created.json()["service_id"]
    assert client.post(f"/api/admin/backup-slots/{sid}/resize", headers=admin, json={"size_gb": 40}).json()["size_gb"] == 40
    assert client.get(f"/api/admin/backup-slots/{sid}", headers=admin).json()["status"] == "active"
    assert client.post(f"/api/admin/backup-slots/{sid}/release", headers=admin).status_code == 409


# --- WHMCS billing surface ---


def _catalog(client, admin):
    family = client.post(
        "/api/product-catalog/families",
        headers=admin,
        json={"name": "Slots", "code": "slot-family", "service_type": "backup_slot", "defaults": {"size_gb": 30}},
    )
    assert family.status_code == 201, family.text
    product = client.post(
        "/api/product-catalog/products",
        headers=admin,
        json={"family_id": family.json()["id"], "name": "Slot 30", "code": "slot-30"},
    )
    assert product.status_code == 201, product.text
    bad = client.post(
        "/api/product-catalog/families",
        headers=admin,
        json={"name": "Bad", "code": "slot-bad", "service_type": "backup_slot", "defaults": {"size_gb": "lots"}},
    )
    assert bad.status_code == 400


def test_whmcs_slot_lifecycle_and_retain_flow(client, db_session, env, test_admin_user):
    admin = _login(client, "admin", "adminpassword123")
    _catalog(client, admin)
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-slots", integration_type="whmcs")
    headers = {"Authorization": f"Bearer {integration.plaintext_api_key}"}

    created = client.post(
        "/api/billing/bare-metal/services",
        headers=headers,
        json={
            "name": "whmcs-slot-1",
            "external_user_id": "whmcs-client-5",
            "external_username": "slotclient",
            "external_email": "slotclient@example.com",
            "service_type": "backup_slot",
            "product_code": "slot-30",
        },
    )
    assert created.status_code == 201, created.text
    slot_id = created.json()["id"]
    assert created.json()["status"] == "active"
    summary = client.get(f"/api/billing/services/{slot_id}/backup-slot", headers=headers).json()
    assert summary["size_gb"] == 30 and summary["occupied"] is False

    # Give that client a VM and a backup, then retain through the billing API.
    owner = ServiceDAO.get_by_id(db_session, slot_id).owner_user
    vm = env.vm(owner, "whmcs-vm", 5300)
    volid = env.pbs.add(5300, "w")
    listed = client.get(f"/api/billing/services/{vm.id}/backup-slots", headers=headers).json()
    assert [s["service_id"] for s in listed["slots"]] == [slot_id]
    retained = client.post(
        f"/api/billing/services/{slot_id}/backup-slot/retain",
        headers=headers,
        json={"source_service_id": vm.id, "volid": volid, "name": "from whmcs"},
    )
    assert retained.status_code == 200, retained.text
    assert (STORAGE, volid, True) in env.pbs.protect_calls

    # Another client's VM is not accepted as a source.
    other = client.post(
        f"/api/billing/services/{slot_id}/backup-slot/retain",
        headers=headers,
        json={"source_service_id": env.alice_vm1.id, "volid": volid},
    )
    assert other.status_code in (403, 404)

    # Package change: refused below the retained backup, allowed above.
    assert client.post(
        f"/api/billing/services/{slot_id}/backup-slot/resize", headers=headers, json={"size_gb": 80}
    ).json()["size_gb"] == 80

    # Suspend blocks new retains but keeps the data; unsuspend restores service.
    assert client.post(f"/api/billing/services/{slot_id}/suspend", headers=headers, json={}).status_code == 200
    db_session.expire_all()
    assert client.get(f"/api/billing/services/{slot_id}/backup-slot", headers=headers).json()["status"] == "suspended"
    blocked = client.post(
        f"/api/billing/services/{slot_id}/backup-slot/release", headers=headers
    )
    assert blocked.status_code == 200  # releasing is allowed: the client is freeing their own data
    assert client.post(f"/api/billing/services/{slot_id}/unsuspend", headers=headers, json={}).status_code == 200

    # Another integration cannot see it.
    rival = BillingIntegrationDAO.create(db_session, name="whmcs-rival", integration_type="whmcs")
    rival_headers = {"Authorization": f"Bearer {rival.plaintext_api_key}"}
    assert client.get(f"/api/billing/services/{slot_id}/backup-slot", headers=rival_headers).status_code in (403, 404)

    # Terminating keeps the retained backup (none now) and starts the grace period.
    volid2 = env.pbs.add(5300, "w2")
    client.post(
        f"/api/billing/services/{slot_id}/backup-slot/retain",
        headers=headers,
        json={"source_service_id": vm.id, "volid": volid2},
    )
    assert client.delete(f"/api/billing/services/{slot_id}", headers=headers).status_code in (200, 204)
    db_session.expire_all()
    row = db_session.query(BackupSlot).filter_by(service_id=slot_id).one()
    assert row.terminated_at is not None and row.destroy_after > row.terminated_at
    assert volid2 in env.pbs.protected and volid2 not in env.pbs.deleted


def test_whmcs_suspended_slot_refuses_retain_and_restore(client, db_session, env, test_admin_user):
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-susp", integration_type="whmcs")
    headers = {"Authorization": f"Bearer {integration.plaintext_api_key}"}
    admin = _login(client, "admin", "adminpassword123")
    _catalog(client, admin)
    created = client.post(
        "/api/billing/bare-metal/services",
        headers=headers,
        json={"name": "s", "external_user_id": "w-7", "external_username": "w7", "external_email": "w7@example.com",
              "service_type": "backup_slot", "product_code": "slot-30"},
    )
    slot_id = created.json()["id"]
    owner = ServiceDAO.get_by_id(db_session, slot_id).owner_user
    vm = env.vm(owner, "susp-vm", 5400)
    volid = env.pbs.add(5400, "x")
    client.post(f"/api/billing/services/{slot_id}/suspend", headers=headers, json={})
    resp = client.post(
        f"/api/billing/services/{slot_id}/backup-slot/retain",
        headers=headers,
        json={"source_service_id": vm.id, "volid": volid},
    )
    assert resp.status_code == 409 and "suspended" in resp.json()["detail"]
