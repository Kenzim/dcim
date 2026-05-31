from app.dao.service_instance_dao import ServiceInstanceDAO
from app.models.billing_integration import BillingIntegration
from app.models.external_user import ExternalUser
from app.models.location import Location
from app.models.server import Server
from app.models.service import Service, ServiceStatus, ServiceType, ProvisioningSource


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_product_catalog_and_vm_plan(client, test_admin_user):
    headers = _admin_headers(client, test_admin_user)

    family = client.post(
        "/api/product-catalog/families",
        headers=headers,
        json={
            "name": "VM Family",
            "code": "vm-family-1",
            "service_type": "vm",
            "provisioning_backend": "proxmox",
            "defaults": {"cpu_count": 2, "ram_mb": 2048},
            "constraints": {"min_disk_gb": 20},
        },
    )
    assert family.status_code == 201, family.text
    family_id = family.json()["id"]

    product = client.post(
        "/api/product-catalog/products",
        headers=headers,
        json={
            "family_id": family_id,
            "name": "VM Small",
            "code": "vm-small",
            "overrides": {"ram_mb": 4096},
        },
    )
    assert product.status_code == 201, product.text

    os_profile = client.post(
        "/api/product-catalog/os-profiles",
        headers=headers,
        json={
            "code": "linux-ubuntu",
            "name": "Ubuntu",
            "os_family": "linux",
            "strategy_name": "stub",
            "strategy_config": {"template": "ubuntu-template"},
        },
    )
    assert os_profile.status_code == 201, os_profile.text
    os_id = os_profile.json()["id"]

    attach = client.post(f"/api/product-catalog/families/{family_id}/os-profiles/{os_id}", headers=headers)
    assert attach.status_code == 200, attach.text

    vm_plan = client.post(
        "/api/proxmox/vm/plan",
        headers=headers,
        json={
            "service_id": 101,
            "product_code": "vm-small",
            "os_code": "linux-ubuntu",
            "context": {"order_id": "abc"},
        },
    )
    assert vm_plan.status_code == 200, vm_plan.text
    payload = vm_plan.json()
    assert payload["strategy_name"] == "stub"
    assert payload["effective_specs"]["cpu_count"] == 2
    assert payload["effective_specs"]["ram_mb"] == 4096


def test_vm_plan_via_vm_template_id(client, test_admin_user):
    """VM plan: vm_template_id → strategy from template os_type (merged model + strategy)."""
    headers = _admin_headers(client, test_admin_user)

    family = client.post("/api/product-catalog/families", headers=headers, json={"name": "Tpl Fam VM"})
    assert family.status_code == 201, family.text
    family_id = family.json()["id"]

    product = client.post(
        "/api/product-catalog/products",
        headers=headers,
        json={"family_id": family_id, "name": "VM Tpl Prod", "code": "vm-tpl-prod-2", "overrides": {}},
    )
    assert product.status_code == 201, product.text
    product_id = product.json()["id"]

    tmpl = client.post(
        "/api/product-catalog/vm-templates",
        headers=headers,
        json={
            "name": "Debian 12 cloud",
            "os_type": "Linux - Cloudinit",
            "proxmox_template_name": "ci-debian12-unique-test",
        },
    )
    assert tmpl.status_code == 201, tmpl.text
    tmpl_id = tmpl.json()["id"]

    upd = client.put(
        f"/api/product-catalog/products/{product_id}",
        headers=headers,
        json={"vm_template_ids": [tmpl_id]},
    )
    assert upd.status_code == 200, upd.text

    vm_plan = client.post(
        "/api/proxmox/vm/plan",
        headers=headers,
        json={"service_id": 202, "product_code": "vm-tpl-prod-2", "vm_template_id": tmpl_id},
    )
    assert vm_plan.status_code == 200, vm_plan.text
    payload = vm_plan.json()
    assert payload["os_code"] == "install-linux-cloudinit"
    assert payload["strategy_name"] == "cloudinit_clone"
    assert payload["strategy_plan"]["mode"] == "cloudinit_clone"
    assert payload["vm_template"]["id"] == tmpl_id
    assert payload["vm_template"]["proxmox_template_name"] == "ci-debian12-unique-test"


def test_ipam_assignment_and_runner_config(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)

    location = Location(name="Proxy Loc", description="proxy")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    integration = BillingIntegration(
        name="Test Billing",
        integration_type="whmcs",
        api_key="key-1",
        enabled=True,
    )
    db_session.add(integration)
    db_session.commit()
    db_session.refresh(integration)

    ext_user = ExternalUser(
        integration_id=integration.id,
        external_user_id="client-1",
        external_username="client1",
        external_email="client1@example.com",
    )
    db_session.add(ext_user)
    db_session.commit()
    db_session.refresh(ext_user)

    server = Server(
        name="proxy-srv",
        server_ip="10.50.0.10",
        location_id=location.id,
        plugin_name="proxmox",
        plugin_config={},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    from app.models.service_bare_metal import ServiceBareMetal

    service = Service(
        name="proxy-service",
        external_service_id="svc-1",
        external_user_id=ext_user.id,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE,
        config={},
        provisioning_source=ProvisioningSource.BILLING,
        bare_metal=ServiceBareMetal(server_id=server.id),
    )
    db_session.add(service)
    db_session.commit()
    db_session.refresh(service)

    ServiceInstanceDAO.create(
        db_session,
        location_id=location.id,
        service_type="proxy",
        name="proxy-runner-loc",
        base_url="http://runner.local:8080",
        api_key="runner-secret",
    )

    subnet = client.post(
        "/api/ipam/subnets",
        headers=headers,
        json={
            "name": "proxy-subnet",
            "cidr": "198.51.100.0/30",
            "location_id": location.id,
            "allocation_strategy": "first_free",
        },
    )
    assert subnet.status_code == 201, subnet.text
    subnet_id = subnet.json()["id"]

    assignment = client.post(
        "/api/ipam/assignments",
        headers=headers,
        json={
            "service_id": service.id,
            "subnet_id": subnet_id,
            "username": "u1",
            "password": "p1",
            "assigned_by": "test",
        },
    )
    assert assignment.status_code == 201, assignment.text
    assigned_ip = assignment.json()["ip_address"]
    assert assigned_ip is not None

    config = client.get(
        "/api/runner/proxy/config",
        headers={"Authorization": "Bearer runner-secret"},
    )
    assert config.status_code == 200, config.text
    data = config.json()
    assert data["location_id"] == location.id
    assert any(row["bind_ip"] == assigned_ip and row["username"] == "u1" for row in data["assignments"])

    history = client.get("/api/ipam/history", headers=headers)
    assert history.status_code == 200
    assert any(item["action"] == "assigned" and item["service_id"] == service.id for item in history.json())
