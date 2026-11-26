"""A failed reseller deploy cleans up only the service that request created."""

import logging
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event

from app.api import reseller as reseller_api
from app.services.provisioning.errors import ProvisioningError

from app.models.boot_task import BootTask, BootTaskStatus
from app.models.installation_task import InstallationStatus, InstallationTask
from app.models.ipam import IPAddress, IPSubnet, ServiceIPAssignment
from app.models.location import Location
from app.models.product_catalog import Product, ProductFamily
from app.models.reseller import Invoice, ProductPrice
from app.models.server_activity import ServerActivity
from app.models.server import Server
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.vm_ip_allocation import VMIPAllocation
from tests.api.test_reseller_api import (
    _allow,
    _create_body,
    _headers,
    _product,
    _reseller,
    _vm_product,
)

EXISTING_IP = "198.51.100.200"
EXISTING_VM_IP = "198.51.100.201"


def _bare_metal_product(db_session):
    family = ProductFamily(
        name="Bare metal family",
        code="bare-metal-family-r5",
        service_type="bare_metal",
        defaults={},
        constraints={},
        enabled=True,
    )
    product = Product(
        family=family,
        name="Bare metal product",
        code="bare-metal-product-r5",
        overrides={},
        enabled=True,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductPrice(
            product_id=product.id, setup_cents=1000, monthly_cents=2000, currency="GBP"
        )
    )
    db_session.commit()
    return product


def _seed_owner(client, db_session, key):
    """Deploy once so the reseller's end client exists; return that client's id."""
    seeded = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key, "seed-deploy"),
        json=_create_body(name="seed-proxy", external_service_id="seed-ext"),
    )
    assert seeded.status_code == 201, seeded.text
    return db_session.get(Service, seeded.json()["service"]["id"]).owner_user_id


def _unbilled_service_with_ips(db_session, owner_id, name, external_id):
    service = Service(
        name=name,
        external_service_id=external_id,
        owner_user_id=owner_id,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE,
        config={},
        product_snapshot={},
    )
    subnet = IPSubnet(name=f"net-{name}", cidr=f"203.0.{abs(hash(name)) % 200}.0/24")
    db_session.add_all([service, subnet])
    db_session.flush()
    address = IPAddress(
        subnet_id=subnet.id, ip_address=f"{EXISTING_IP}-{name}", state="assigned"
    )
    allocation = VMIPAllocation(
        ip_address=f"{EXISTING_VM_IP}-{name}",
        subnet_mask="255.255.255.0",
        gateway="198.51.100.1",
        bridge_name="vmbr0",
        enabled=True,
        assigned_service_id=service.id,
    )
    db_session.add_all([address, allocation])
    db_session.flush()
    db_session.add(ServiceIPAssignment(service_id=service.id, ip_id=address.id))
    db_session.commit()
    return service.id, address.id, allocation.id


def _assert_untouched(db_session, service_id, address_id, allocation_id):
    db_session.expire_all()
    assert db_session.get(Service, service_id) is not None
    assert db_session.get(VMIPAllocation, allocation_id).assigned_service_id == service_id
    assert db_session.get(IPAddress, address_id).state == "assigned"
    assert (
        db_session.query(ServiceIPAssignment)
        .filter(ServiceIPAssignment.service_id == service_id)
        .count()
        == 1
    )


@pytest.mark.parametrize("match", ["name", "external_service_id"])
def test_name_taken_leaves_the_clients_existing_unbilled_service_alone(
    client, db_session, match
):
    reseller, key = _reseller(db_session, f"r5-{match}", credit_cents=10_000)
    product = _product(db_session)
    _allow(db_session, reseller, product)
    owner_id = _seed_owner(client, db_session, key)
    existing = _unbilled_service_with_ips(
        db_session, owner_id, "unbilled-existing", "unbilled-ext"
    )
    if match == "name":
        body = _create_body(name="unbilled-existing", external_service_id="fresh-ext")
    else:
        body = _create_body(name="seed-proxy", external_service_id="unbilled-ext")

    response = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key, "r5-deploy"),
        json=body,
    )

    assert response.status_code in (400, 409), response.text
    assert "already exists" in response.text
    _assert_untouched(db_session, *existing)
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 7000
    assert [i.status.value for i in db_session.query(Invoice).all()].count("void") == 1


def test_failed_deploy_after_create_removes_its_own_service_and_ips(
    client, db_session, monkeypatch
):
    reseller, key = _reseller(db_session, "r5-own", credit_cents=10_000)
    product = _product(db_session)
    _allow(db_session, reseller, product)
    owner_id = _seed_owner(client, db_session, key)
    bystander = _unbilled_service_with_ips(db_session, owner_id, "bystander", "by-ext")
    own_allocation = VMIPAllocation(
        ip_address="198.51.100.77",
        subnet_mask="255.255.255.0",
        gateway="198.51.100.1",
        bridge_name="vmbr0",
        enabled=True,
    )
    db_session.add(own_allocation)
    db_session.commit()
    own_allocation_id = own_allocation.id

    def fail_finalize(db, *, service, **_kwargs):
        db.get(VMIPAllocation, own_allocation_id).assigned_service_id = service.id
        # The SQLite test schema has a service-or-server check on activity rows
        # that the migrated MariaDB schema lacks; drop them so the delete can run.
        db.query(ServerActivity).filter(
            ServerActivity.service_id == service.id
        ).delete()
        db.commit()
        raise RuntimeError("finalize exploded")

    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.finalize_deploy", fail_finalize
    )
    with pytest.raises(RuntimeError, match="finalize exploded"):
        client.post(
            "/api/reseller/bare-metal/services",
            headers=_headers(key, "r5-own-deploy"),
            json=_create_body(name="doomed-proxy", external_service_id="doomed-ext"),
        )

    db_session.expire_all()
    assert db_session.query(Service).filter(Service.name == "doomed-proxy").count() == 0
    assert db_session.get(VMIPAllocation, own_allocation_id).assigned_service_id is None
    _assert_untouched(db_session, *bystander)


def _fail_finalize(monkeypatch):
    def fail_finalize(*_args, **_kwargs):
        raise RuntimeError("finalize exploded")

    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.finalize_deploy", fail_finalize
    )


def _retry_setup(client, db_session, key_name):
    """Seed a reseller, a bare-metal product and an unbilled order to be retried."""
    reseller, key = _reseller(db_session, key_name, credit_cents=10_000)
    product = _product(db_session)
    _allow(db_session, reseller, product)
    owner_id = _seed_owner(client, db_session, key)
    bare_metal_product = _bare_metal_product(db_session)
    _allow(db_session, reseller, bare_metal_product)
    retried = Service(
        name="retried-order",
        external_service_id="retried-ext",
        owner_user_id=owner_id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.PENDING,
        config={"keep": "me"},
        product_snapshot={},
    )
    db_session.add(retried)
    db_session.commit()
    body = _create_body(name="retried-order", external_service_id="retried-ext")
    body["service_type"] = "bare_metal"
    body["product_code"] = bare_metal_product.code
    return key, body, retried.id


def _post_retry(client, key, body, idempotency):
    return client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key, idempotency),
        json=body,
    )


def _server(db_session):
    location = Location(name="R5 location", description="Test")
    db_session.add(location)
    db_session.commit()
    server = Server(
        name="r5-server",
        server_ip="172.16.9.10",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"host": "172.16.9.10"},
    )
    db_session.add(server)
    db_session.commit()
    return server.id


def _queue_install(db_session, server_id):
    boot = BootTask(server_id=server_id, status=BootTaskStatus.PENDING)
    db_session.add(boot)
    db_session.commit()
    task = InstallationTask(
        server_id=server_id,
        boot_task_id=boot.id,
        status=InstallationStatus.PENDING,
    )
    db_session.add(task)
    db_session.commit()
    return boot.id, task.id


def test_resumed_service_survives_a_failure_at_billing_setup(
    client, db_session, monkeypatch
):
    key, body, retried_id = _retry_setup(client, db_session, "r5-retry")
    _fail_finalize(monkeypatch)

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _post_retry(client, key, body, "r5-retry-deploy")

    db_session.expire_all()
    assert db_session.get(Service, retried_id) is not None


def test_a_kept_resumed_service_is_named_in_a_warning(
    client, db_session, monkeypatch, caplog
):
    key, body, retried_id = _retry_setup(client, db_session, "r5-warn")
    _fail_finalize(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.api.reseller"):
        with pytest.raises(RuntimeError, match="finalize exploded"):
            _post_retry(client, key, body, "r5-warn-deploy")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        f"kept resumed service {retried_id} ('retried-order')" in message
        for message in warnings
    ), warnings


def test_resume_queued_install_is_cancelled_and_the_service_kept(
    client, db_session, monkeypatch
):
    key, body, retried_id = _retry_setup(client, db_session, "r5-queue")
    server_id = _server(db_session)
    queued = {}

    def resume(db, service, req, actor):
        queued["ids"] = _queue_install(db, server_id)
        return service

    monkeypatch.setattr(
        "app.services.provisioning.service.resume_bare_metal_order", resume
    )
    _fail_finalize(monkeypatch)

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _post_retry(client, key, body, "r5-queue-deploy")

    db_session.expire_all()
    boot_id, task_id = queued["ids"]
    assert db_session.get(Service, retried_id).config == {"keep": "me"}
    assert db_session.get(InstallationTask, task_id).status == InstallationStatus.CANCELLED
    assert db_session.get(BootTask, boot_id).status == BootTaskStatus.CANCELLED


def test_a_failing_resume_step_keeps_the_existing_service(
    client, db_session, monkeypatch
):
    key, body, retried_id = _retry_setup(client, db_session, "r5-resume-fails")
    server_id = _server(db_session)
    queued = {}

    def resume(db, service, req, actor):
        queued["ids"] = _queue_install(db, server_id)
        raise RuntimeError("resume exploded")

    monkeypatch.setattr(
        "app.services.provisioning.service.resume_bare_metal_order", resume
    )

    with pytest.raises(RuntimeError, match="resume exploded"):
        _post_retry(client, key, body, "r5-resume-fails-deploy")

    db_session.expire_all()
    _, task_id = queued["ids"]
    assert db_session.get(Service, retried_id) is not None
    assert db_session.get(InstallationTask, task_id).status == InstallationStatus.CANCELLED


def test_failure_inside_create_after_the_service_row_is_written_is_cleaned_up(
    client, db_session, monkeypatch
):
    reseller, key = _reseller(db_session, "r5-inside", credit_cents=10_000)
    product = _vm_product(db_session)
    _allow(db_session, reseller, product)
    bystander = _unbilled_service_with_ips(db_session, None, "bystander", "by-ext")

    def fail_ip_assignment(*_args, **_kwargs):
        raise RuntimeError("ip assignment exploded")

    monkeypatch.setattr(
        "app.services.provisioning.vm._assign_vm_ip_or_fail", fail_ip_assignment
    )
    with pytest.raises(RuntimeError, match="ip assignment exploded"):
        client.post(
            "/api/reseller/vm/services",
            headers=_headers(key, "r5-inside-deploy"),
            json={
                "name": "half-made-vm",
                "external_service_id": "half-made-ext",
                "external_user_id": "vm-client",
                "product_code": product.code,
                "auto_provision": False,
            },
        )

    db_session.expire_all()
    assert db_session.query(Service).filter(Service.name == "half-made-vm").count() == 0
    _assert_untouched(db_session, *bystander)


def _resume_queuing(monkeypatch, db_session, key, body, retried_id, mutate=None):
    """Patch the resume step to queue an install, optionally changing its state."""
    queued = {}
    server_id = _server(db_session)

    def resume(db, service, req, actor):
        queued["ids"] = _queue_install(db, server_id)
        if mutate is not None:
            mutate(db, *queued["ids"])
        return service

    monkeypatch.setattr(
        "app.services.provisioning.service.resume_bare_metal_order", resume
    )
    return queued, server_id


def test_started_tasks_of_this_request_are_left_alone(
    client, db_session, monkeypatch
):
    key, body, retried_id = _retry_setup(client, db_session, "r5-started")

    def start(db, boot_id, task_id):
        db.get(BootTask, boot_id).status = BootTaskStatus.IN_PROGRESS
        db.get(InstallationTask, task_id).status = InstallationStatus.IN_PROGRESS
        db.commit()

    queued, _ = _resume_queuing(monkeypatch, db_session, key, body, retried_id, start)
    _fail_finalize(monkeypatch)

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _post_retry(client, key, body, "r5-started-deploy")

    db_session.expire_all()
    boot_id, task_id = queued["ids"]
    assert db_session.get(BootTask, boot_id).status == BootTaskStatus.IN_PROGRESS
    assert (
        db_session.get(InstallationTask, task_id).status
        == InstallationStatus.IN_PROGRESS
    )


def test_only_this_requests_tasks_are_cancelled(client, db_session, monkeypatch):
    key, body, retried_id = _retry_setup(client, db_session, "r5-scope")
    queued, server_id = _resume_queuing(
        monkeypatch, db_session, key, body, retried_id
    )
    other_server = Server(
        name="r5-other",
        server_ip="172.16.9.11",
        location_id=_server_location(db_session),
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(other_server)
    db_session.commit()
    other_boot, other_task = _queue_install(db_session, other_server.id)
    older_boot = BootTask(server_id=server_id, status=BootTaskStatus.PENDING)
    db_session.add(older_boot)
    db_session.commit()
    older_boot_id = older_boot.id
    _fail_finalize(monkeypatch)

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _post_retry(client, key, body, "r5-scope-deploy")

    db_session.expire_all()
    boot_id, task_id = queued["ids"]
    assert db_session.get(BootTask, boot_id).status == BootTaskStatus.CANCELLED
    assert db_session.get(BootTask, older_boot_id).status == BootTaskStatus.PENDING
    assert db_session.get(BootTask, other_boot).status == BootTaskStatus.PENDING
    assert (
        db_session.get(InstallationTask, other_task).status
        == InstallationStatus.PENDING
    )


def _server_location(db_session):
    return db_session.query(Location).first().id


def test_cancelling_a_boot_task_requests_a_dhcp_refresh(
    client, db_session, monkeypatch, _no_background_dhcp_sync
):
    key, body, retried_id = _retry_setup(client, db_session, "r5-dhcp")
    queued, server_id = _resume_queuing(
        monkeypatch, db_session, key, body, retried_id
    )
    _fail_finalize(monkeypatch)
    _no_background_dhcp_sync.clear()

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _post_retry(client, key, body, "r5-dhcp-deploy")

    assert _no_background_dhcp_sync == [server_id]


def _record_hardware_calls(monkeypatch, boot_launcher_calls):
    calls = []

    def recorder(name):
        def record(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"clean-up must not call {name}")

        return record

    for name in (
        "schedule_launch",
        "schedule_release",
        "release_boot_media",
        "launch_boot_task",
        "_power_cycle",
        "_launch_vmedia",
    ):
        monkeypatch.setattr(f"app.services.boot_launcher.{name}", recorder(name))
    monkeypatch.setattr(
        "app.plugins.registry.PluginRegistry.get_plugin", recorder("get_plugin")
    )

    def pdu_recorder(name):
        def record(*args, **kwargs):
            calls.append(name)
            return MagicMock()

        return record

    monkeypatch.setattr(
        "app.plugins.pdu.registry.PduPluginRegistry.get_plugin",
        pdu_recorder("pdu_get_plugin"),
    )
    monkeypatch.setattr(
        "app.plugins.pdu.registry.get_pdu_registry", pdu_recorder("get_pdu_registry")
    )
    return calls


@pytest.mark.parametrize("resumed", [False, True])
def test_cleanup_makes_no_power_boot_device_or_bmc_call(
    db_session, monkeypatch, boot_launcher_calls, resumed
):
    from app.api.reseller import _DeployFacts, _DeployTrail, _cleanup_failed_service

    server_id = _server(db_session)
    boot_id, task_id = _queue_install(db_session, server_id)
    service = Service(
        name="cleanup-subject",
        external_service_id="cleanup-ext",
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.PENDING,
        config={},
        product_snapshot={},
    )
    db_session.add(service)
    db_session.commit()
    hardware_calls = _record_hardware_calls(monkeypatch, boot_launcher_calls)
    trail = _DeployTrail(
        service_ids=[] if resumed else [service.id],
        boot_task_ids=[boot_id],
        installation_task_ids=[task_id],
        resumed=(service.id, service.name) if resumed else None,
    )

    _cleanup_failed_service(db_session, _DeployFacts(1, 2, "ext", "name", 3, 100, "GBP"), trail)

    db_session.expire_all()
    assert hardware_calls == []
    assert boot_launcher_calls == {"launch": [], "release": []}
    assert db_session.get(BootTask, boot_id).status == BootTaskStatus.CANCELLED
    assert (db_session.get(Service, service.id) is None) is (not resumed)


def test_the_flush_listener_is_removed_afterwards(client, db_session, monkeypatch):
    key, body, _ = _retry_setup(client, db_session, "r5-listener")
    registered = []
    original = event.listen

    def counting_listen(target, name, fn, *args, **kwargs):
        registered.append((target, name, fn))
        return original(target, name, fn, *args, **kwargs)

    monkeypatch.setattr("app.api.reseller.event.listen", counting_listen)
    _fail_finalize(monkeypatch)

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _post_retry(client, key, body, "r5-listener-deploy")

    assert registered
    for target, name, fn in registered:
        assert not event.contains(target, name, fn)


@pytest.mark.parametrize("failure", ["provisioning_error", "unexpected_error"])
def test_a_failed_deploy_logs_reseller_owner_external_id_and_name(
    client, db_session, monkeypatch, caplog, failure
):
    key, body, retried_id = _retry_setup(client, db_session, f"r5-{failure}")
    owner_id = db_session.get(Service, retried_id).owner_user_id

    def resume_fails(db, service, req, actor):
        if failure == "provisioning_error":
            raise ProvisioningError("resume_failed", "resume refused")
        raise RuntimeError("resume exploded")

    monkeypatch.setattr(
        "app.services.provisioning.service.resume_bare_metal_order", resume_fails
    )

    with caplog.at_level(logging.WARNING, logger="app.api.reseller"):
        if failure == "provisioning_error":
            response = _post_retry(client, key, body, f"r5-{failure}-deploy")
            assert response.status_code >= 400
        else:
            with pytest.raises(RuntimeError, match="resume exploded"):
                _post_retry(client, key, body, f"r5-{failure}-deploy")

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "retried-ext" in m
        and "retried-order" in m
        and f"owner {owner_id}" in m
        and "reseller " in m
        for m in messages
    ), messages


@pytest.mark.parametrize("failure", ["provisioning_error", "unexpected_error"])
def test_the_failure_is_logged_before_the_clean_up_runs(
    client, db_session, monkeypatch, caplog, failure
):
    key, body, _ = _retry_setup(client, db_session, f"r5-order-{failure}")

    def resume_fails(db, service, req, actor):
        if failure == "provisioning_error":
            raise ProvisioningError("resume_failed", "resume refused")
        raise RuntimeError("resume exploded")

    monkeypatch.setattr(
        "app.services.provisioning.service.resume_bare_metal_order", resume_fails
    )
    seen_at_cleanup = []
    real_cleanup = reseller_api._cleanup_failed_service

    def spy(db, reseller, trail):
        seen_at_cleanup.append(
            [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        )
        return real_cleanup(db, reseller, trail)

    monkeypatch.setattr(reseller_api, "_cleanup_failed_service", spy)

    with caplog.at_level(logging.WARNING, logger="app.api.reseller"):
        try:
            _post_retry(client, key, body, f"r5-order-{failure}-deploy")
        except RuntimeError:
            pass

    assert len(seen_at_cleanup) == 1
    assert any("Reseller deploy failed" in m for m in seen_at_cleanup[0])
