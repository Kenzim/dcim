"""Coverage for the protected reseller administration API."""

from datetime import datetime, timedelta, timezone

from app.models.product_catalog import Product, ProductFamily
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    NotificationOutbox,
    NotificationOutboxStatus,
    PaymentStatus,
    Reseller,
    ServiceBilling,
)
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User


def _admin_headers(client) -> dict:
    response = client.post(
        "/api/users/login",
        json={"username": "admin", "password": "adminpassword123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _product(db_session, suffix: str = "admin") -> Product:
    family = ProductFamily(
        name=f"Reseller admin family {suffix}",
        code=f"reseller-admin-family-{suffix}",
        service_type="vm",
        defaults={"cpu_cores": 2, "ram_mb": 2048, "disk_gb": 30},
        constraints={},
        enabled=True,
    )
    product = Product(
        family=family,
        name=f"Reseller admin product {suffix}",
        code=f"reseller-admin-product-{suffix}",
        overrides={},
        enabled=True,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def _create_group(client, headers, suffix: str = "gold") -> dict:
    response = client.post(
        "/api/admin/reseller-groups",
        headers=headers,
        json={
            "name": f"Group {suffix}",
            "code": f"group-{suffix}",
            "description": "Test group",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_reseller(client, headers, suffix: str, group_id=None) -> dict:
    response = client.post(
        "/api/admin/resellers",
        headers=headers,
        json={
            "username": f"reseller-admin-{suffix}",
            "email": f"reseller-admin-{suffix}@example.com",
            "password": "reseller-password",
            "group_id": group_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_admin_can_inspect_and_retry_cycles_and_outbox(
    client, db_session, test_admin_user
):
    headers = _admin_headers(client)
    created = _create_reseller(client, headers, "cycle-admin")
    reseller = db_session.get(Reseller, created["id"])
    owner = User(
        username="cycle-admin-client",
        email="cycle-admin-client@example.com",
        reseller_id=reseller.id,
    )
    service = Service(
        name="cycle-admin-service",
        owner_user=owner,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE,
    )
    db_session.add(service)
    db_session.flush()
    due = datetime.now(timezone.utc) + timedelta(days=10)
    billing = ServiceBilling(
        service_id=service.id,
        reseller_id=reseller.id,
        setup_price_cents=0,
        monthly_price_cents=1000,
        currency="USD",
        next_charge_at=due,
    )
    invoice = Invoice(
        invoice_number=880001,
        reseller_id=reseller.id,
        service_id=service.id,
        purpose=InvoicePurpose.CYCLE_CHARGE,
        status=InvoiceStatus.OVERDUE,
        amount_cents=1000,
        currency="USD",
        due_at=due,
    )
    db_session.add_all([billing, invoice])
    db_session.flush()
    cycle = BillingCycle(
        service_billing_id=billing.id,
        invoice_id=invoice.id,
        due_at=due,
        amount_cents=1000,
        currency="USD",
        state=BillingCycleState.GRACE,
        attempts=2,
        next_retry_at=due,
        grace_until=due + timedelta(days=7),
    )
    notification = NotificationOutbox(
        idempotency_key="admin-outbox-retry",
        reseller_id=reseller.id,
        recipient=reseller.user.email,
        event="grace_warning",
        template="grace_warning",
        data={},
        status=NotificationOutboxStatus.SKIPPED,
    )
    db_session.add_all([cycle, notification])
    db_session.commit()

    cycles = client.get(
        f"/api/admin/resellers/billing-cycles?reseller_id={reseller.id}",
        headers=headers,
    )
    assert cycles.status_code == 200, cycles.text
    assert cycles.json()[0]["id"] == cycle.id
    retried = client.post(
        f"/api/admin/resellers/billing-cycles/{cycle.id}/retry",
        headers=headers,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "grace"

    outbox = client.get(
        f"/api/admin/resellers/notification-outbox?reseller_id={reseller.id}",
        headers=headers,
    )
    assert outbox.status_code == 200, outbox.text
    retried_notification = client.post(
        f"/api/admin/resellers/notification-outbox/{notification.id}/retry",
        headers=headers,
    )
    assert retried_notification.status_code == 200
    assert retried_notification.json()["status"] == "queued"


def test_every_admin_route_requires_admin(client, db_session, test_admin_user):
    assert client.get("/api/admin/resellers").status_code == 401
    assert client.get("/api/admin/reseller-groups").status_code == 401
    assert client.get("/api/admin/resellers/prices").status_code == 401
    assert client.get("/api/admin/resellers/quotas").status_code == 401

    ordinary = User(
        username="not-reseller-admin",
        email="not-reseller-admin@example.com",
        is_admin=False,
    )
    ordinary.set_password("ordinary-password")
    db_session.add(ordinary)
    db_session.commit()
    login = client.post(
        "/api/users/login",
        json={"username": ordinary.username, "password": "ordinary-password"},
    )
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/api/admin/resellers", headers=headers).status_code == 403


def test_group_and_reseller_crud_one_time_key_and_rotation(
    client, db_session, test_admin_user
):
    headers = _admin_headers(client)
    group = _create_group(client, headers)
    duplicate = client.post(
        "/api/admin/reseller-groups",
        headers=headers,
        json={"name": "group GOLD", "code": "other-code"},
    )
    assert duplicate.status_code == 409

    created = _create_reseller(client, headers, "account", group["id"])
    reseller_id = created["id"]
    first_key = created["api_key"]
    assert first_key.startswith("rsk_")
    assert "api_key_hash" not in created
    assert created["user"]["is_reseller"] is True
    assert created["group_id"] == group["id"]

    detail = client.get(
        f"/api/admin/resellers/{reseller_id}", headers=headers
    )
    assert detail.status_code == 200, detail.text
    assert "api_key" not in detail.json()
    assert "api_key_hash" not in detail.json()
    assert detail.json()["api_key_prefix"] == first_key[:16]

    in_use = client.delete(
        f"/api/admin/reseller-groups/{group['id']}", headers=headers
    )
    assert in_use.status_code == 409
    disabled = client.put(
        f"/api/admin/reseller-groups/{group['id']}",
        headers=headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    profile_update = client.put(
        f"/api/admin/resellers/{reseller_id}",
        headers=headers,
        json={
            "group_id": group["id"],
            "charge_preference": "payment_first",
            "nonpayment_policy": "suspend_all",
            "billing_hold": True,
            "billing_hold_reason": "manual review",
        },
    )
    assert profile_update.status_code == 200, profile_update.text
    assert profile_update.json()["charge_preference"] == "payment_first"
    assert profile_update.json()["nonpayment_policy"] == "suspend_all"
    assert profile_update.json()["billing_hold"] is True
    assert profile_update.json()["billing_hold_reason"] == "manual review"
    assert profile_update.json()["billing_hold_at"] is not None

    rotated = client.post(
        f"/api/admin/resellers/{reseller_id}/rotate-key", headers=headers
    )
    assert rotated.status_code == 200, rotated.text
    second_key = rotated.json()["api_key"]
    assert second_key != first_key
    assert (
        client.get(
            "/api/reseller/products",
            headers={"Authorization": f"Bearer {first_key}"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/reseller/products",
            headers={"Authorization": f"Bearer {second_key}"},
        ).status_code
        == 200
    )

    suspended = client.put(
        f"/api/admin/resellers/{reseller_id}",
        headers=headers,
        json={"status": "suspended"},
    )
    assert suspended.status_code == 200
    assert (
        client.get(
            "/api/reseller/products",
            headers={"Authorization": f"Bearer {second_key}"},
        ).status_code
        == 401
    )
    db_session.refresh(db_session.get(Reseller, reseller_id))


def test_base_group_prices_and_access_inheritance(
    client, db_session, test_admin_user
):
    headers = _admin_headers(client)
    product = _product(db_session, "pricing")
    group = _create_group(client, headers, "pricing")
    reseller = _create_reseller(client, headers, "pricing", group["id"])

    floating = client.put(
        f"/api/admin/resellers/prices/{product.id}",
        headers=headers,
        json={"setup_cents": 100.5, "monthly_cents": 200, "currency": "USD"},
    )
    assert floating.status_code == 422
    wrong_currency = client.put(
        f"/api/admin/resellers/prices/{product.id}",
        headers=headers,
        json={"setup_cents": 100, "monthly_cents": 200, "currency": "GBP"},
    )
    assert wrong_currency.status_code == 422

    base = client.put(
        f"/api/admin/resellers/prices/{product.id}",
        headers=headers,
        json={"setup_cents": 100, "monthly_cents": 200, "currency": "USD"},
    )
    assert base.status_code == 200, base.text
    override = client.put(
        f"/api/admin/reseller-groups/{group['id']}/prices/{product.id}",
        headers=headers,
        json={"setup_cents": None, "monthly_cents": 150},
    )
    assert override.status_code == 200, override.text
    assert override.json()["effective_setup_cents"] == 100
    assert override.json()["effective_monthly_cents"] == 150

    initial_access = client.get(
        f"/api/admin/resellers/{reseller['id']}/product-access",
        headers=headers,
    ).json()
    row = next(item for item in initial_access if item["product_id"] == product.id)
    assert row["effective_allowed"] is False
    assert row["effective_source"] == "default_deny"
    assert row["default_deny"] is True

    group_access = client.put(
        f"/api/admin/reseller-groups/{group['id']}/product-access/{product.id}",
        headers=headers,
        json={"allowed": True},
    )
    assert group_access.status_code == 200
    inherited = client.get(
        f"/api/admin/resellers/{reseller['id']}/product-access",
        headers=headers,
    ).json()
    row = next(item for item in inherited if item["product_id"] == product.id)
    assert row["effective_allowed"] is True
    assert row["effective_source"] == "group"

    direct = client.put(
        f"/api/admin/resellers/{reseller['id']}/product-access/{product.id}",
        headers=headers,
        json={"allowed": False},
    )
    assert direct.status_code == 200
    row = next(item for item in direct.json() if item["product_id"] == product.id)
    assert row["effective_allowed"] is False
    assert row["effective_source"] == "direct"

    deleted = client.delete(
        f"/api/admin/resellers/{reseller['id']}/product-access/{product.id}",
        headers=headers,
    )
    assert deleted.status_code == 204
    inherited_again = client.get(
        f"/api/admin/resellers/{reseller['id']}/product-access",
        headers=headers,
    ).json()
    row = next(
        item for item in inherited_again if item["product_id"] == product.id
    )
    assert row["effective_allowed"] is True
    assert row["effective_source"] == "group"


def test_quota_validation_usage_and_owner_cross_validation(
    client, db_session, test_admin_user
):
    headers = _admin_headers(client)
    product = _product(db_session, "quota-admin")
    reseller = _create_reseller(client, headers, "quota-admin")

    both_owners = client.post(
        "/api/admin/resellers/quotas",
        headers=headers,
        json={
            "reseller_id": reseller["id"],
            "group_id": 123,
            "scope_type": "product",
            "scope_id": product.id,
            "max_services": 2,
        },
    )
    assert both_owners.status_code == 422
    no_limits = client.post(
        "/api/admin/resellers/quotas",
        headers=headers,
        json={
            "reseller_id": reseller["id"],
            "scope_type": "product",
            "scope_id": product.id,
        },
    )
    assert no_limits.status_code == 422
    missing_scope = client.post(
        "/api/admin/resellers/quotas",
        headers=headers,
        json={
            "reseller_id": reseller["id"],
            "scope_type": "server_group",
            "scope_id": 999999,
            "max_services": 1,
        },
    )
    assert missing_scope.status_code == 404

    service = Service(
        name="quota-admin-service",
        service_type=ServiceType.VM,
        status=ServiceStatus.ACTIVE,
        product_code=product.code,
        product_snapshot={
            "effective_specs": {
                "cpu_cores": 2,
                "ram_mb": 2048,
                "disk_gb": 30,
            }
        },
    )
    db_session.add(service)
    db_session.flush()
    db_session.add(
        ServiceBilling(
            service_id=service.id,
            reseller_id=reseller["id"],
            product_id=product.id,
            setup_price_cents=0,
            monthly_price_cents=100,
            currency="USD",
        )
    )
    db_session.commit()

    created = client.post(
        "/api/admin/resellers/quotas",
        headers=headers,
        json={
            "reseller_id": reseller["id"],
            "scope_type": "product",
            "scope_id": product.id,
            "max_services": 3,
            "max_cpu_cores": 8,
            "max_ram_mb": 8192,
            "max_disk_gb": 100,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["usage"] == {
        "services": 1,
        "cpu_cores": 2,
        "ram_mb": 2048,
        "disk_gb": 30,
    }
    assert created.json()["source"] == "direct"
    assert created.json()["is_effective"] is True

    listed = client.get(
        f"/api/admin/resellers/quotas?reseller_id={reseller['id']}"
        "&effective_only=true",
        headers=headers,
    )
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [created.json()["id"]]


def test_credit_audit_invoice_payment_and_ledger_reads(
    client, db_session, test_admin_user
):
    headers = _admin_headers(client)
    reseller = _create_reseller(client, headers, "finance")
    reseller_id = reseller["id"]

    credited = client.post(
        f"/api/admin/resellers/{reseller_id}/credit-adjustments",
        headers=headers,
        json={
            "amount_cents": 5000,
            "reason": "Opening balance approved by finance",
            "idempotency_key": "admin-opening-balance",
        },
    )
    assert credited.status_code == 200, credited.text
    assert credited.json()["new_balance_cents"] == 5000
    entry = credited.json()["entry"]
    assert entry["amount_cents"] == 5000
    assert entry["created_by_user_id"] == test_admin_user.id
    assert entry["metadata"]["admin_user_id"] == test_admin_user.id
    assert entry["metadata"]["reason"] == "Opening balance approved by finance"

    replay = client.post(
        f"/api/admin/resellers/{reseller_id}/credit-adjustments",
        headers=headers,
        json={
            "amount_cents": 5000,
            "reason": "Opening balance approved by finance",
            "idempotency_key": "admin-opening-balance",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["entry"]["id"] == entry["id"]
    assert replay.json()["new_balance_cents"] == 5000

    insufficient = client.post(
        f"/api/admin/resellers/{reseller_id}/credit-adjustments",
        headers=headers,
        json={"amount_cents": -5001, "reason": "Invalid debit test"},
    )
    assert insufficient.status_code == 409
    assert insufficient.json()["detail"]["code"] == "insufficient_credit"

    invoice = Invoice(
        invoice_number=700001,
        reseller_id=reseller_id,
        purpose=InvoicePurpose.CREDIT_TOPUP,
        status=InvoiceStatus.PAID,
        amount_cents=5000,
        currency="USD",
        description="Opening balance",
    )
    payment = Payment(
        invoice=invoice,
        gateway="manual",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=5000,
        currency="USD",
        external_ref="manual-finance-1",
        payment_metadata={"admin": True},
    )
    db_session.add_all([invoice, payment])
    db_session.commit()

    invoices = client.get(
        f"/api/admin/resellers/invoices?reseller_id={reseller_id}",
        headers=headers,
    )
    assert invoices.status_code == 200
    assert invoices.json()[0]["id"] == invoice.id
    assert invoices.json()[0]["reseller"]["username"] == reseller["user"]["username"]
    assert invoices.json()[0]["allocated_cents"] == 5000
    detail = client.get(
        f"/api/admin/resellers/invoices/{invoice.id}", headers=headers
    )
    assert detail.status_code == 200
    assert detail.json()["payments"][0]["id"] == payment.id
    assert detail.json()["payments"][0]["refundable"] is False
    assert "manual" in detail.json()["payments"][0]["refund_block_reason"]

    payments = client.get(
        f"/api/admin/resellers/payments?reseller_id={reseller_id}",
        headers=headers,
    )
    assert payments.status_code == 200
    assert payments.json()[0]["reseller_id"] == reseller_id
    payment_detail = client.get(
        f"/api/admin/resellers/payments/{payment.id}", headers=headers
    )
    assert payment_detail.status_code == 200
    assert payment_detail.json()["metadata"] == {"admin": True}
    assert payment_detail.json()["refunded_at"] is None

    blocked = client.post(
        f"/api/admin/resellers/payments/{payment.id}/refund",
        headers=headers,
        json={"reason": "should fail"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "not_refundable"

    ledger = client.get(
        f"/api/admin/resellers/{reseller_id}/ledger", headers=headers
    )
    assert ledger.status_code == 200
    assert [row["id"] for row in ledger.json()] == [entry["id"]]
