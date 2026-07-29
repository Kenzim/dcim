import pytest

from app.core.reseller_auth import issue_reseller_api_key
from app.models.product_catalog import (
    OSProfile,
    Product,
    ProductFamily,
    ProductFamilyOSProfile,
    ProductVMTemplate,
    VMTemplate,
)
from app.models.proxmox_inventory import ProxmoxCluster, ProxmoxNode
from app.models.reseller import (
    CreditLedgerEntry,
    CreditLedgerEntryType,
    Invoice,
    ProductPrice,
    Reseller,
    ResellerProductAccess,
    ServiceBilling,
    StockQuota,
    StockQuotaScope,
)
from app.models.server_group import ServerGroup
from app.models.service import Service
from app.models.user import User
from app.models.vm_ip_allocation import VMIPAllocation
from app.services.credit_ledger_service import CreditLedgerService


def _reseller(db_session, suffix, credit_cents=0):
    user = User(
        username=f"api-reseller-{suffix}",
        email=f"api-reseller-{suffix}@example.com",
        is_reseller=True,
    )
    reseller = Reseller(user=user)
    db_session.add(reseller)
    db_session.flush()
    key = issue_reseller_api_key(db_session, reseller)
    db_session.commit()
    if credit_cents:
        CreditLedgerService.credit(
            db_session,
            reseller.id,
            credit_cents,
            idempotency_key=f"seed-{suffix}",
        )
        db_session.commit()
    return reseller, key


def _product(db_session, suffix="proxy", setup=1000, monthly=2000):
    family = ProductFamily(
        name=f"Proxy family {suffix}",
        code=f"proxy-family-{suffix}",
        service_type="http_proxy",
        defaults={
            "cpu_cores": 2,
            "ram_mb": 2048,
            "disk_gb": 20,
        },
        constraints={},
        enabled=True,
    )
    product = Product(
        family=family,
        name=f"Proxy product {suffix}",
        code=f"proxy-product-{suffix}",
        overrides={},
        enabled=True,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductPrice(
            product_id=product.id,
            setup_cents=setup,
            monthly_cents=monthly,
            currency="GBP",
        )
    )
    db_session.commit()
    return product


def _vm_product(db_session):
    family = ProductFamily(
        name="VM family",
        code="vm-family-reseller",
        service_type="vm",
        provisioning_backend="proxmox",
        defaults={"cpu_cores": 2, "ram_mb": 4096, "disk_gb": 40},
        constraints={},
        enabled=True,
    )
    product = Product(
        family=family,
        name="VM product",
        code="vm-product-reseller",
        overrides={},
        enabled=True,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add_all(
        [
            ProductPrice(
                product_id=product.id,
                setup_cents=500,
                monthly_cents=2500,
                currency="GBP",
            ),
            VMIPAllocation(
                ip_address="198.51.100.44",
                subnet_mask="255.255.255.0",
                gateway="198.51.100.1",
                bridge_name="vmbr0",
                enabled=True,
            ),
        ]
    )
    db_session.commit()
    return product


def _allow(db_session, reseller, product, **quota):
    db_session.add_all(
        [
            ResellerProductAccess(
                reseller_id=reseller.id,
                product_id=product.id,
                allowed=True,
            ),
            StockQuota(
                reseller_id=reseller.id,
                scope_type=StockQuotaScope.PRODUCT,
                scope_id=product.id,
                max_services=quota.pop("max_services", 10),
                **quota,
            ),
        ]
    )
    db_session.commit()


def _headers(key, idempotency_key=None):
    result = {"Authorization": f"Bearer {key}"}
    if idempotency_key:
        result["Idempotency-Key"] = idempotency_key
    return result


def _create_body(name="reseller-proxy", external_service_id="ext-service-1"):
    return {
        "name": name,
        "external_service_id": external_service_id,
        "external_user_id": "client-1",
        "external_username": "client-one",
        "external_email": "client-one@example.com",
        "service_type": "http_proxy",
        "product_code": "proxy-product-proxy",
    }


@pytest.mark.parametrize(
    "hold,balance,code",
    [
        (True, 0, "billing_hold"),
        (False, -1, "negative_balance"),
    ],
)
def test_deploy_is_blocked_by_billing_hold_or_negative_balance(
    client, db_session, hold, balance, code
):
    reseller, key = _reseller(db_session, f"blocked-{code}")
    reseller.billing_hold = hold
    reseller.billing_hold_reason = "nonpayment" if hold else None
    reseller.cached_balance_cents = balance
    db_session.commit()

    response = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key, f"blocked-{code}"),
        json=_create_body(
            name=f"blocked-{code}",
            external_service_id=f"blocked-{code}",
        ),
    )
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == code


def test_products_are_default_deny_and_include_effective_integer_price(
    client, db_session
):
    reseller, key = _reseller(db_session, "products")
    product = _product(db_session)

    generic_auth = client.get("/api/users/me", headers=_headers(key))
    assert generic_auth.status_code == 401

    denied = client.get("/api/reseller/products", headers=_headers(key))
    assert denied.status_code == 200, denied.text
    assert denied.json() == []

    _allow(db_session, reseller, product)
    allowed = client.get("/api/reseller/products", headers=_headers(key))
    assert allowed.status_code == 200, allowed.text
    assert len(allowed.json()) == 1
    row = allowed.json()[0]
    assert row["code"] == product.code
    assert row["setup_cents"] == 1000
    assert row["monthly_cents"] == 2000
    assert row["currency"] == "GBP"
    assert isinstance(row["monthly_cents"], int)


def test_product_filter_and_loader_resources_follow_products_and_quotas(
    client, db_session
):
    reseller, key = _reseller(db_session, "loaders")
    proxy_product = _product(db_session, suffix="loader")
    vm_product = _vm_product(db_session)
    cluster_one = ProxmoxCluster(
        name="Cluster One",
        api_url="https://cluster-one.example",
        username="root@pam",
        password="not-returned",
        enabled=True,
    )
    cluster_two = ProxmoxCluster(
        name="Cluster Two",
        api_url="https://cluster-two.example",
        username="root@pam",
        password="not-returned",
        enabled=True,
    )
    cluster_disabled = ProxmoxCluster(
        name="Cluster Disabled",
        api_url="https://cluster-disabled.example",
        username="root@pam",
        password="not-returned",
        enabled=False,
    )
    cluster_one.nodes.append(ProxmoxNode(node_name="node-a", enabled=True))
    group_one = ServerGroup(name="Group One")
    group_two = ServerGroup(name="Group Two")
    db_session.add_all(
        [
            cluster_one,
            cluster_two,
            cluster_disabled,
            group_one,
            group_two,
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            ResellerProductAccess(
                reseller_id=reseller.id,
                product_id=proxy_product.id,
                allowed=True,
            ),
            ResellerProductAccess(
                reseller_id=reseller.id,
                product_id=vm_product.id,
                allowed=True,
            ),
            StockQuota(
                reseller_id=reseller.id,
                scope_type=StockQuotaScope.SERVER_GROUP,
                scope_id=group_two.id,
                max_services=2,
            ),
            StockQuota(
                reseller_id=reseller.id,
                scope_type=StockQuotaScope.PROXMOX_CLUSTER,
                scope_id=cluster_one.id,
                max_services=2,
            ),
        ]
    )
    db_session.commit()

    vm_products = client.get(
        "/api/reseller/products?service_type=vm", headers=_headers(key)
    )
    assert vm_products.status_code == 200, vm_products.text
    assert [row["code"] for row in vm_products.json()] == [vm_product.code]

    clusters = client.get(
        "/api/reseller/proxmox/clusters", headers=_headers(key)
    )
    assert clusters.status_code == 200, clusters.text
    assert clusters.json() == [
        {
            "id": cluster_one.id,
            "name": "Cluster One",
            "nodes": [{"node_name": "node-a", "enabled": True}],
        }
    ]
    assert "api_url" not in clusters.text
    assert "not-returned" not in clusters.text

    groups = client.get(
        "/api/reseller/server-groups", headers=_headers(key)
    )
    assert groups.status_code == 200, groups.text
    assert [row["id"] for row in groups.json()] == [group_two.id]


def test_loader_resources_are_hidden_without_compatible_quota(
    client, db_session
):
    reseller, key = _reseller(db_session, "loader-no-quota")
    vm_product = _vm_product(db_session)
    db_session.add_all(
        [
            ResellerProductAccess(
                reseller_id=reseller.id,
                product_id=vm_product.id,
                allowed=True,
            ),
            ProxmoxCluster(
                name="Hidden Cluster",
                api_url="https://hidden.example",
                username="root@pam",
                password="hidden",
                enabled=True,
            ),
        ]
    )
    db_session.commit()

    response = client.get(
        "/api/reseller/proxmox/clusters", headers=_headers(key)
    )
    assert response.status_code == 200
    assert response.json() == []


def test_product_loader_returns_only_linked_enabled_os_and_templates(
    client, db_session
):
    reseller, key = _reseller(db_session, "product-options")
    product = _vm_product(db_session)
    _allow(db_session, reseller, product)
    linked_os = OSProfile(
        code="linked-linux",
        name="Linked Linux",
        os_family="linux",
        enabled=True,
    )
    disabled_os = OSProfile(
        code="disabled-linux",
        name="Disabled Linux",
        os_family="linux",
        enabled=False,
    )
    unlinked_os = OSProfile(
        code="unlinked-linux",
        name="Unlinked Linux",
        os_family="linux",
        enabled=True,
    )
    linked_template = VMTemplate(
        code="linked-template",
        name="Linked Template",
        os_type="Linux - Cloudinit",
        proxmox_template_name="linked-template",
        enabled=True,
    )
    unlinked_template = VMTemplate(
        code="unlinked-template",
        name="Unlinked Template",
        os_type="Linux - Cloudinit",
        proxmox_template_name="unlinked-template",
        enabled=True,
    )
    db_session.add_all(
        [
            linked_os,
            disabled_os,
            unlinked_os,
            linked_template,
            unlinked_template,
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            ProductFamilyOSProfile(
                family_id=product.family_id,
                os_profile_id=linked_os.id,
            ),
            ProductFamilyOSProfile(
                family_id=product.family_id,
                os_profile_id=disabled_os.id,
            ),
            ProductVMTemplate(
                product_id=product.id,
                vm_template_id=linked_template.id,
            ),
        ]
    )
    db_session.commit()

    response = client.get(
        f"/api/reseller/products/{product.code}",
        headers=_headers(key),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [row["code"] for row in payload["os_profiles"]] == [
        "linked-linux"
    ]
    assert [row["code"] for row in payload["vm_templates"]] == [
        "linked-template"
    ]
    assert payload["checkout_os_mode"] == "vm_template"


def test_openapi_documents_nested_create_envelope_and_stable_402(client):
    operation = client.get("/openapi.json").json()["paths"][
        "/api/reseller/vm/services"
    ]["post"]
    assert (
        operation["responses"]["201"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        == "#/components/schemas/ResellerDeployResponse"
    )
    detail = operation["responses"]["402"]["content"]["application/json"][
        "example"
    ]["detail"]
    assert detail == {
        "code": "insufficient_credit",
        "required_cents": 3000,
        "available_cents": 500,
        "shortfall_cents": 2500,
        "invoice_id": 42,
    }


def test_credit_deploy_charges_setup_and_first_month_and_is_idempotent(
    client, db_session
):
    reseller, key = _reseller(
        db_session, "credit-success", credit_cents=10_000
    )
    product = _product(db_session)
    _allow(db_session, reseller, product)
    body = _create_body()

    first = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key, "deploy-one"),
        json=body,
    )
    assert first.status_code == 201, first.text
    first_json = first.json()
    assert first_json["invoice"]["amount_cents"] == 3000
    assert first_json["invoice"]["status"] == "paid"
    assert first_json["idempotent_replay"] is False

    service_id = first_json["service"]["id"]
    billing = (
        db_session.query(ServiceBilling)
        .filter(ServiceBilling.service_id == service_id)
        .one()
    )
    assert billing.setup_price_cents == 1000
    assert billing.monthly_price_cents == 2000
    assert billing.currency == "GBP"
    assert billing.next_charge_at is not None

    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 7000
    charge_count = (
        db_session.query(CreditLedgerEntry)
        .filter(
            CreditLedgerEntry.reseller_id == reseller.id,
            CreditLedgerEntry.entry_type == CreditLedgerEntryType.CHARGE,
        )
        .count()
    )
    assert charge_count == 1

    replay = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key, "deploy-one"),
        json=body,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["service"]["id"] == service_id
    assert replay.json()["invoice"]["id"] == first_json["invoice"]["id"]
    assert replay.json()["idempotent_replay"] is True
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 7000


def test_vm_deploy_uses_shared_core_and_allocates_vm_ip(
    client, db_session
):
    reseller, key = _reseller(
        db_session, "vm-deploy", credit_cents=10_000
    )
    product = _vm_product(db_session)
    _allow(db_session, reseller, product)

    created = client.post(
        "/api/reseller/vm/services",
        headers=_headers(key, "vm-deploy-one"),
        json={
            "name": "reseller-vm",
            "external_service_id": "reseller-vm-1",
            "external_user_id": "vm-client",
            "product_code": product.code,
            "auto_provision": False,
        },
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["service"]["service_type"] == "vm"
    assert payload["service"]["vm_ip_address"] == "198.51.100.44"
    assert payload["invoice"]["amount_cents"] == 3000


def test_insufficient_credit_returns_reusable_402_invoice_and_no_service(
    client, db_session
):
    reseller, key = _reseller(
        db_session, "credit-short", credit_cents=500
    )
    product = _product(db_session)
    _allow(db_session, reseller, product)
    body = _create_body(
        name="short-credit-proxy", external_service_id="short-service"
    )

    first = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key, "short-one"),
        json=body,
    )
    assert first.status_code == 402, first.text
    detail = first.json()["detail"]
    assert detail == {
        "code": "insufficient_credit",
        "required_cents": 3000,
        "available_cents": 500,
        "shortfall_cents": 2500,
        "invoice_id": detail["invoice_id"],
    }
    assert db_session.query(Service).count() == 0
    invoice = db_session.get(Invoice, detail["invoice_id"])
    assert invoice.status.value == "open"

    replay = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key, "short-one"),
        json=body,
    )
    assert replay.status_code == 402
    assert replay.json()["detail"]["invoice_id"] == invoice.id
    assert db_session.query(Invoice).count() == 1
    assert db_session.query(Service).count() == 0


def test_failed_provisioning_refunds_credit_and_voids_invoice(
    client, db_session, monkeypatch
):
    reseller, key = _reseller(
        db_session, "provision-fail", credit_cents=10_000
    )
    product = _product(db_session)
    _allow(db_session, reseller, product)

    async def fail_provision(**_kwargs):
        raise RuntimeError("provisioning exploded")

    monkeypatch.setattr(
        "app.api.reseller.provision_bare_metal_service", fail_provision
    )
    headers = _headers(key, "failing-deploy")
    body = _create_body(
        name="failing-proxy", external_service_id="failing-service"
    )
    with pytest.raises(RuntimeError, match="provisioning exploded"):
        client.post(
            "/api/reseller/bare-metal/services",
            headers=headers,
            json=body,
        )

    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 10_000
    invoice = db_session.query(Invoice).one()
    assert invoice.status.value == "void"
    assert db_session.query(Service).count() == 0
    entries = (
        db_session.query(CreditLedgerEntry)
        .filter(CreditLedgerEntry.reseller_id == reseller.id)
        .all()
    )
    assert [entry.amount_cents for entry in entries] == [10_000, -3000, 3000]


def test_quota_count_and_resources_reject_before_debit(client, db_session):
    reseller, key = _reseller(
        db_session, "quota", credit_cents=10_000
    )
    product = _product(db_session)
    _allow(db_session, reseller, product, max_services=0)

    count_rejected = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key),
        json=_create_body(
            name="count-quota-proxy", external_service_id="count-quota"
        ),
    )
    assert count_rejected.status_code == 409, count_rejected.text
    assert "Service-count quota exceeded" in count_rejected.json()["detail"]
    assert db_session.query(Invoice).count() == 0

    quota = db_session.query(StockQuota).one()
    quota.max_services = 10
    quota.max_cpu_cores = 1
    db_session.commit()
    resource_rejected = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key),
        json=_create_body(
            name="resource-quota-proxy",
            external_service_id="resource-quota",
        ),
    )
    assert resource_rejected.status_code == 409, resource_rejected.text
    assert "CPU quota exceeded" in resource_rejected.json()["detail"]
    assert db_session.query(Invoice).count() == 0


def test_missing_product_and_resource_quota_is_default_deny(
    client, db_session
):
    reseller, key = _reseller(
        db_session, "no-quota", credit_cents=10_000
    )
    product = _product(db_session)
    db_session.add(
        ResellerProductAccess(
            reseller_id=reseller.id,
            product_id=product.id,
            allowed=True,
        )
    )
    db_session.commit()

    rejected = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key),
        json=_create_body(
            name="no-quota-proxy", external_service_id="no-quota"
        ),
    )
    assert rejected.status_code == 403, rejected.text
    assert "No product or matching resource quota" in rejected.json()["detail"]
    assert db_session.query(Invoice).count() == 0


def test_service_lookup_is_cross_reseller_404(client, db_session):
    first, first_key = _reseller(
        db_session, "tenant-one", credit_cents=10_000
    )
    second, second_key = _reseller(
        db_session, "tenant-two", credit_cents=10_000
    )
    product = _product(db_session)
    _allow(db_session, first, product)
    _allow(db_session, second, product)

    created = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(first_key),
        json=_create_body(
            name="tenant-one-proxy", external_service_id="tenant-service"
        ),
    )
    assert created.status_code == 201, created.text
    service_id = created.json()["service"]["id"]

    hidden = client.get(
        f"/api/reseller/services/{service_id}",
        headers=_headers(second_key),
    )
    assert hidden.status_code == 404

    visible = client.get(
        f"/api/reseller/services/{service_id}",
        headers=_headers(first_key),
    )
    assert visible.status_code == 200, visible.text
    for method, suffix, body in [
        ("get", "status", None),
        ("post", "power", {"action": "off"}),
        ("post", "suspend", {"reason": "cross-tenant"}),
        ("post", "unsuspend", {"reason": "cross-tenant"}),
        ("post", "portal-sso", None),
    ]:
        response = getattr(client, method)(
            f"/api/reseller/services/{service_id}/{suffix}",
            headers=_headers(second_key),
            **({"json": body} if body is not None else {}),
        )
        assert response.status_code == 404, (suffix, response.text)
    hidden_delete = client.delete(
        f"/api/reseller/services/{service_id}",
        headers=_headers(second_key),
    )
    assert hidden_delete.status_code == 404


def test_reseller_proxy_lifecycle_routes_delegate_with_tenant_scope(
    client, db_session, monkeypatch
):
    reseller, key = _reseller(
        db_session, "lifecycle", credit_cents=10_000
    )
    product = _product(db_session)
    _allow(db_session, reseller, product)
    created = client.post(
        "/api/reseller/bare-metal/services",
        headers=_headers(key),
        json=_create_body(
            name="lifecycle-proxy", external_service_id="lifecycle-service"
        ),
    )
    assert created.status_code == 201, created.text
    service_id = created.json()["service"]["id"]

    suspended = client.post(
        f"/api/reseller/services/{service_id}/suspend",
        headers=_headers(key),
        json={"reason": "test"},
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["status"] == "suspended"

    unsuspended = client.post(
        f"/api/reseller/services/{service_id}/unsuspend",
        headers=_headers(key),
        json={"reason": "test"},
    )
    assert unsuspended.status_code == 200, unsuspended.text
    assert unsuspended.json()["status"] == "active"

    service_status = client.get(
        f"/api/reseller/services/{service_id}/status",
        headers=_headers(key),
    )
    assert service_status.status_code == 200, service_status.text
    assert service_status.json()["power_available"] is False

    monkeypatch.setattr(
        "app.api.reseller.mint_sso_ticket", lambda user_id: f"ticket-{user_id}"
    )
    portal = client.post(
        f"/api/reseller/services/{service_id}/portal-sso",
        headers=_headers(key),
    )
    assert portal.status_code == 200, portal.text
    assert portal.json()["token"].startswith("ticket-")
    assert portal.json()["redeem_path"] == "/api/client/sso/redeem"

    terminated = client.delete(
        f"/api/reseller/services/{service_id}", headers=_headers(key)
    )
    assert terminated.status_code == 204, terminated.text
    billing = (
        db_session.query(ServiceBilling)
        .filter(ServiceBilling.service_id == service_id)
        .one()
    )
    assert billing.status.value == "cancelled"
