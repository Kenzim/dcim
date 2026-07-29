import pytest

from app.core.reseller_auth import issue_reseller_api_key
from app.models.product_catalog import Product, ProductFamily
from app.models.reseller import (
    GatewayWebhookEvent,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    ProductPrice,
    Reseller,
    ResellerClientProductPermission,
    ResellerProductAccess,
    ServiceBilling,
    StockQuota,
    StockQuotaScope,
)
from app.models.service import Service
from app.models.user import User
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_service import InvoiceService
from app.services.payments.base import (
    GatewayChargeResult,
    GatewayCustomer,
    GatewayPaymentMethod,
    GatewayResultStatus,
    GatewaySetupIntent,
    GatewayVaultSetup,
    GatewayWebhookAction,
    NormalizedGatewayWebhookEvent,
    PaymentGatewayError,
)
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.payments.registry import payment_gateway_registry


class FakeStripeGateway:
    name = "stripe"

    def __init__(self):
        self.event = {}
        self.method_customer = "cus_test"
        self.charge_result = GatewayChargeResult(
            status=GatewayResultStatus.SUCCEEDED,
            external_ref="pi_api_success",
        )

    def create_customer(self, **_kwargs):
        return GatewayCustomer(external_ref="cus_test")

    def create_setup_intent(self, **_kwargs):
        return GatewaySetupIntent(
            external_ref="seti_test", client_secret="seti_test_secret"
        )

    def get_payment_method(self, *, method_ref):
        return GatewayPaymentMethod(
            external_ref=method_ref,
            customer_ref=self.method_customer,
            method_type="card",
            label="Visa •••• 4242",
            brand="visa",
            last4="4242",
        )

    def detach_payment_method(self, **_kwargs):
        return None

    def charge_off_session(self, **_kwargs):
        return self.charge_result

    def construct_webhook_event(self, *, payload, signature):
        if signature != "valid":
            raise ValueError("bad signature")
        return self.event

    def normalize_webhook_event(self, event):
        event_type = event["type"]
        obj = event.get("data", {}).get("object", {})
        action = GatewayWebhookAction.IGNORED
        external_ref = obj.get("id")
        amount_cents = None
        currency = None
        failure_code = None
        if event_type == "payment_intent.succeeded":
            action = GatewayWebhookAction.PAYMENT_COMPLETED
            amount_cents = obj.get("amount_received", obj.get("amount"))
            currency = str(obj.get("currency", "")).upper() or None
        elif event_type == "payment_intent.payment_failed":
            action = GatewayWebhookAction.PAYMENT_FAILED
            failure_code = obj.get("last_payment_error", {}).get("code")
        elif event_type == "charge.dispute.created":
            action = GatewayWebhookAction.PAYMENT_DISPUTED
            external_ref = obj.get("payment_intent")
        return NormalizedGatewayWebhookEvent(
            event_id=event["id"],
            event_type=event_type,
            action=action,
            external_ref=external_ref,
            amount_cents=amount_cents,
            currency=currency,
            failure_code=failure_code,
        )


class FakePayPalGateway:
    name = "paypal"

    def __init__(self):
        self.normalized_event = NormalizedGatewayWebhookEvent(
            event_id="ignored",
            event_type="IGNORED",
            action=GatewayWebhookAction.IGNORED,
        )
        self._reseller_id = None
        self.detached = []
        self.charge_result = GatewayChargeResult(
            status=GatewayResultStatus.SUCCEEDED,
            external_ref="CAPTURE-api",
        )

    def create_vault_setup(self, *, reseller_id, customer_ref=None):
        return GatewayVaultSetup(
            external_ref=f"SETUP-{reseller_id}",
            approval_url=f"https://paypal.test/approve/{reseller_id}",
            customer_ref=customer_ref or f"customer_{reseller_id}",
        )

    def complete_vault_setup(self, *, setup_token_ref):
        self._reseller_id = int(setup_token_ref.rsplit("-", 1)[-1])
        return self.get_payment_method(method_ref=f"VAULT-{self._reseller_id}")

    def get_payment_method(self, *, method_ref):
        return GatewayPaymentMethod(
            external_ref=method_ref,
            customer_ref=f"customer_{self._reseller_id}",
            method_type="paypal",
            label=f"PayPal (payer-{self._reseller_id}@example.test)",
            brand="paypal",
            metadata={
                "merchant_customer_id": (
                    f"rackflow-reseller-{self._reseller_id}"
                )
            },
        )

    def detach_payment_method(self, *, method_ref):
        self.detached.append(method_ref)
        return None

    def charge_off_session(self, **_kwargs):
        return self.charge_result

    def construct_webhook_event(
        self, *, payload, signature=None, headers=None
    ):
        lowered = {key.lower(): value for key, value in (headers or {}).items()}
        if lowered.get("paypal-transmission-sig") != "valid":
            raise PaymentGatewayError("bad signature")
        return {"verified": True}

    def normalize_webhook_event(self, _event):
        return self.normalized_event


@pytest.fixture
def fake_stripe():
    previous = payment_gateway_registry.maybe_get("stripe")
    gateway = FakeStripeGateway()
    payment_gateway_registry.register(gateway)
    try:
        yield gateway
    finally:
        payment_gateway_registry.unregister("stripe")
        if previous is not None:
            payment_gateway_registry.register(previous)


@pytest.fixture
def fake_paypal():
    previous = payment_gateway_registry.maybe_get("paypal")
    gateway = FakePayPalGateway()
    payment_gateway_registry.register(gateway)
    try:
        yield gateway
    finally:
        payment_gateway_registry.unregister("paypal")
        if previous is not None:
            payment_gateway_registry.register(previous)


def _login(client, db_session, suffix, *, reseller=True):
    user = User(
        username=f"panel-{suffix}",
        email=f"panel-{suffix}@example.com",
        is_reseller=reseller,
    )
    user.set_password("panel-password")
    account = Reseller(user=user) if reseller else None
    db_session.add(account or user)
    db_session.commit()
    response = client.post(
        "/api/users/login",
        json={"username": user.username, "password": "panel-password"},
    )
    assert response.status_code == 200, response.text
    return (
        account,
        {"Authorization": f"Bearer {response.json()['token']}"},
    )


def _panel_product(db_session, suffix="phase8"):
    family = ProductFamily(
        name=f"Panel family {suffix}",
        code=f"panel-family-{suffix}",
        service_type="http_proxy",
        defaults={"cpu_cores": 2, "ram_mb": 2048, "disk_gb": 20},
        constraints={},
        enabled=True,
    )
    product = Product(
        family=family,
        name=f"Panel product {suffix}",
        code=f"panel-product-{suffix}",
        overrides={},
        enabled=True,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductPrice(
            product_id=product.id,
            setup_cents=1000,
            monthly_cents=2000,
            currency="USD",
        )
    )
    db_session.commit()
    return product


def test_session_role_tenant_scope_and_topup_limits(client, db_session):
    first, headers = _login(client, db_session, "first")
    first.billing_hold = True
    first.billing_hold_reason = "nonpayment"
    db_session.commit()
    second, _ = _login(client, db_session, "second")
    _plain, plain_headers = _login(
        client, db_session, "plain", reseller=False
    )
    other_invoice = InvoiceService.create_topup(
        db_session, reseller_id=second.id, amount_cents=1000
    )
    db_session.commit()

    assert (
        client.get("/api/reseller-panel/dashboard", headers=plain_headers).status_code
        == 403
    )
    dashboard = client.get("/api/reseller-panel/dashboard", headers=headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["billing_hold"] is True
    assert dashboard.json()["billing_hold_reason"] == "nonpayment"
    assert (
        client.get(
            f"/api/reseller-panel/invoices/{other_invoice.id}",
            headers=headers,
        ).status_code
        == 404
    )
    too_small = client.post(
        "/api/reseller-panel/top-up-invoices",
        headers=headers,
        json={"amount_cents": 499},
    )
    assert too_small.status_code == 422
    created = client.post(
        "/api/reseller-panel/top-up-invoices",
        headers=headers,
        json={"amount_cents": 500},
    )
    assert created.status_code == 201, created.text
    assert created.json()["reseller_id"] == first.id
    assert created.json()["currency"] == "USD"


def test_product_permissions_visibility_and_provision_denial(
    client, db_session
):
    first, headers = _login(client, db_session, "catalog-first")
    second, second_headers = _login(client, db_session, "catalog-second")
    product = _panel_product(db_session, "visibility")
    db_session.add(
        ResellerProductAccess(
            reseller_id=first.id, product_id=product.id, allowed=True
        )
    )
    db_session.commit()

    listed = client.get("/api/reseller-panel/products", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["client_visible"] is True
    assert (
        listed.json()[0]["rackflow_permission_ceiling"][
            "proxy.rotate_credentials"
        ]
        is False
    )
    assert (
        client.put(
            f"/api/reseller-panel/products/{product.id}/client-permissions",
            headers=second_headers,
            json={"visible": False, "permissions": {}},
        ).status_code
        == 404
    )
    excessive = client.put(
        f"/api/reseller-panel/products/{product.id}/client-permissions",
        headers=headers,
        json={
            "visible": True,
            "permissions": {"proxy.rotate_credentials": True},
        },
    )
    assert excessive.status_code == 422
    hidden = client.put(
        f"/api/reseller-panel/products/{product.id}/client-permissions",
        headers=headers,
        json={
            "visible": False,
            "permissions": {"proxy.view_credentials": True},
        },
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["client_visible"] is False

    api_key = issue_reseller_api_key(db_session, first)
    db_session.commit()
    external_headers = {"Authorization": f"Bearer {api_key}"}
    assert client.get(
        "/api/reseller/products", headers=external_headers
    ).json() == []
    denied = client.post(
        "/api/reseller/bare-metal/services",
        headers=external_headers,
        json={
            "name": "hidden-product",
            "external_service_id": "hidden-product-1",
            "external_user_id": "hidden-client",
            "service_type": "http_proxy",
            "product_code": product.code,
        },
    )
    assert denied.status_code == 404

    visible = client.put(
        f"/api/reseller-panel/products/{product.id}/client-permissions",
        headers=headers,
        json={
            "visible": True,
            "permissions": {"proxy.view_credentials": False},
        },
    )
    assert visible.status_code == 200
    external_products = client.get(
        "/api/reseller/products", headers=external_headers
    ).json()
    assert external_products[0]["client_permissions"][
        "proxy.view_credentials"
    ] is False


def test_clients_services_quotas_and_dashboard_are_tenant_scoped(
    client, db_session
):
    first, headers = _login(client, db_session, "resources-first")
    second, _ = _login(client, db_session, "resources-second")
    product = _panel_product(db_session, "resources")
    owner = User(
        username="resources-client",
        email="resources-client@example.com",
        reseller_id=first.id,
        external_user_id="client-one",
    )
    other_owner = User(
        username="resources-other-client",
        email="resources-other-client@example.com",
        reseller_id=second.id,
        external_user_id="client-two",
    )
    service = Service(
        name="first-service",
        owner_user=owner,
        service_type="http_proxy",
        status="active",
        product_code=product.code,
        product_snapshot={
            "effective_specs": {
                "cpu_cores": 2,
                "ram_mb": 2048,
                "disk_gb": 20,
            }
        },
    )
    other_service = Service(
        name="other-service",
        owner_user=other_owner,
        service_type="http_proxy",
        status="active",
    )
    db_session.add_all([service, other_service])
    db_session.flush()
    db_session.add_all(
        [
            ServiceBilling(
                service_id=service.id,
                reseller_id=first.id,
                product_id=product.id,
                setup_price_cents=1000,
                monthly_price_cents=2000,
                currency="USD",
            ),
            ServiceBilling(
                service_id=other_service.id,
                reseller_id=second.id,
                setup_price_cents=0,
                monthly_price_cents=9999,
                currency="USD",
            ),
            StockQuota(
                reseller_id=first.id,
                scope_type=StockQuotaScope.PRODUCT,
                scope_id=product.id,
                max_services=2,
                max_cpu_cores=4,
                max_ram_mb=4096,
                max_disk_gb=40,
            ),
        ]
    )
    db_session.commit()

    clients = client.get("/api/reseller-panel/clients", headers=headers)
    assert clients.status_code == 200
    assert [row["id"] for row in clients.json()] == [owner.id]
    assert "password" not in clients.json()[0]
    assert (
        client.get(
            f"/api/reseller-panel/clients/{other_owner.id}", headers=headers
        ).status_code
        == 404
    )
    services = client.get("/api/reseller-panel/services", headers=headers)
    assert [row["id"] for row in services.json()] == [service.id]
    assert (
        client.get(
            f"/api/reseller-panel/services/{other_service.id}",
            headers=headers,
        ).status_code
        == 404
    )
    quotas = client.get("/api/reseller-panel/quotas", headers=headers).json()
    assert quotas[0]["source"] == "reseller"
    assert quotas[0]["usage"]["services"] == 1
    assert quotas[0]["remaining"]["cpu_cores"] == 2
    dashboard = client.get(
        "/api/reseller-panel/dashboard", headers=headers
    ).json()
    assert dashboard["client_count"] == 1
    assert dashboard["service_count"] == 1
    assert dashboard["monthly_cost_cents"] == 2000


def test_api_key_rotation_requires_password_and_returns_plaintext_once(
    client, db_session
):
    reseller, headers = _login(client, db_session, "rotate-key")
    before = client.get("/api/reseller-panel/api-key", headers=headers)
    assert before.status_code == 200
    assert "api_key_hash" not in before.json()
    assert (
        client.post(
            "/api/reseller-panel/api-key/rotate",
            headers=headers,
            json={"current_password": "wrong"},
        ).status_code
        == 403
    )
    rotated = client.post(
        "/api/reseller-panel/api-key/rotate",
        headers=headers,
        json={"current_password": "panel-password"},
    )
    assert rotated.status_code == 200, rotated.text
    plaintext = rotated.json()["api_key"]
    assert plaintext.startswith("rsk_")
    after = client.get("/api/reseller-panel/api-key", headers=headers).json()
    assert "api_key" not in after
    assert "api_key_hash" not in after
    assert after["prefix"] == plaintext[:16]

    reseller.user.password = None
    db_session.commit()
    blank = client.post(
        "/api/reseller-panel/api-key/rotate",
        headers=headers,
        json={"current_password": "anything"},
    )
    assert blank.status_code == 409
    assert "Contact an administrator" in blank.json()["detail"]


def test_payment_config_exposes_only_public_values(
    client, db_session, monkeypatch
):
    _reseller, headers = _login(client, db_session, "safe-config")
    monkeypatch.setattr("app.core.config.settings.stripe_secret_key", "sk_secret")
    monkeypatch.setattr(
        "app.core.config.settings.stripe_publishable_key", "pk_public"
    )
    monkeypatch.setattr("app.core.config.settings.paypal_client_id", "paypal-id")
    monkeypatch.setattr(
        "app.core.config.settings.paypal_client_secret", "paypal-secret"
    )
    monkeypatch.setattr("app.core.config.settings.usdt_chain_id", 1)
    monkeypatch.setattr(
        "app.core.config.settings.usdt_contract_address",
        "0x0000000000000000000000000000000000000001",
    )
    response = client.get(
        "/api/reseller-panel/payment-config", headers=headers
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["stripe"]["publishable_key"] == "pk_public"
    assert payload["paypal"]["client_id"] == "paypal-id"
    assert payload["usdt"]["network"] == "ethereum"
    serialized = response.text
    assert "sk_secret" not in serialized
    assert "paypal-secret" not in serialized
    assert "api_key_hash" not in serialized


def test_setup_register_ownership_and_pay_topup(
    client, db_session, fake_stripe
):
    reseller, headers = _login(client, db_session, "payments")
    setup = client.post(
        "/api/reseller-panel/stripe/setup-intent", headers=headers
    )
    assert setup.status_code == 200
    assert setup.json() == {"client_secret": "seti_test_secret"}

    registered = client.post(
        "/api/reseller-panel/payment-methods",
        headers=headers,
        json={"payment_method_id": "pm_owned", "tier": 2},
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["last4"] == "4242"
    assert "provider_method_ref" not in registered.json()

    invoice = InvoiceService.create_topup(
        db_session, reseller_id=reseller.id, amount_cents=1500
    )
    db_session.commit()
    paid = client.post(
        f"/api/reseller-panel/invoices/{invoice.id}/pay",
        headers=headers,
        json={"payment_method_id": registered.json()["id"]},
    )
    assert paid.status_code == 200, paid.text
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 1500

    action_invoice = InvoiceService.create_topup(
        db_session, reseller_id=reseller.id, amount_cents=500
    )
    db_session.commit()
    fake_stripe.charge_result = GatewayChargeResult(
        status=GatewayResultStatus.REQUIRES_ACTION,
        external_ref="pi_api_action",
        client_secret="pi_api_action_secret",
    )
    action = client.post(
        f"/api/reseller-panel/invoices/{action_invoice.id}/pay",
        headers=headers,
        json={"payment_method_id": registered.json()["id"]},
    )
    assert action.status_code == 402
    assert action.json()["detail"]["pending_action"] is True
    assert action.json()["detail"]["client_secret"] == "pi_api_action_secret"
    assert "client_secret" not in action_invoice.payments[0].payment_metadata

    fake_stripe.method_customer = "cus_someone_else"
    rejected = client.post(
        "/api/reseller-panel/payment-methods",
        headers=headers,
        json={"payment_method_id": "pm_foreign"},
    )
    assert rejected.status_code == 403


def test_webhook_signature_success_dedupe_and_failure(
    client, db_session, fake_stripe
):
    reseller, _headers = _login(client, db_session, "webhook")
    invoice = InvoiceService.create_topup(
        db_session, reseller_id=reseller.id, amount_cents=2000
    )
    payment = Payment(
        invoice_id=invoice.id,
        gateway="stripe",
        status=PaymentStatus.PENDING,
        amount_cents=2000,
        currency="USD",
        external_ref="pi_webhook",
    )
    db_session.add(payment)
    db_session.commit()

    fake_stripe.event = {
        "id": "evt_success",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_webhook",
                "amount_received": 2000,
                "currency": "usd",
            }
        },
    }
    invalid = client.post(
        "/api/reseller-panel/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "invalid"},
    )
    assert invalid.status_code == 400
    first = client.post(
        "/api/reseller-panel/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "valid"},
    )
    replay = client.post(
        "/api/reseller-panel/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "valid"},
    )
    assert first.json() == {"received": True, "duplicate": False}
    assert replay.json() == {"received": True, "duplicate": True}
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 2000
    assert len(reseller.ledger_entries) == 1

    failed_invoice = InvoiceService.create_topup(
        db_session, reseller_id=reseller.id, amount_cents=500
    )
    failed_invoice.status = InvoiceStatus.PENDING_ACTION
    failed_payment = Payment(
        invoice_id=failed_invoice.id,
        gateway="stripe",
        status=PaymentStatus.PENDING,
        amount_cents=500,
        currency="USD",
        external_ref="pi_failed",
    )
    db_session.add(failed_payment)
    db_session.commit()
    fake_stripe.event = {
        "id": "evt_failed",
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_failed",
                "last_payment_error": {"code": "card_declined"},
            }
        },
    }
    response = client.post(
        "/api/reseller-panel/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "valid"},
    )
    assert response.status_code == 200
    assert failed_payment.status == PaymentStatus.FAILED
    assert failed_invoice.status == InvoiceStatus.OPEN


def test_dispute_debits_balance_negative(
    client, db_session, fake_stripe
):
    reseller, _headers = _login(client, db_session, "dispute")
    invoice = InvoiceService.create_deploy(
        db_session,
        reseller_id=reseller.id,
        amount_cents=1000,
        description="Deploy",
    )
    payment = Payment(
        invoice_id=invoice.id,
        gateway="stripe",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=1000,
        currency="USD",
        external_ref="pi_disputed",
    )
    db_session.add(payment)
    db_session.flush()
    PaymentOrchestrator.finalize_success(db_session, payment)
    db_session.commit()
    assert invoice.status == InvoiceStatus.PAID

    fake_stripe.event = {
        "id": "evt_dispute",
        "type": "charge.dispute.created",
        "data": {"object": {"id": "dp_test", "payment_intent": "pi_disputed"}},
    }
    response = client.post(
        "/api/reseller-panel/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "valid"},
    )
    assert response.status_code == 200, response.text
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == -1000
    assert payment.status == PaymentStatus.REFUNDED
    assert invoice.status == InvoiceStatus.FAILED


def test_deploy_shortfall_uses_saved_method_fallback(
    client, db_session, fake_stripe
):
    user = User(
        username="deploy-fallback",
        email="deploy-fallback@example.com",
        is_reseller=True,
    )
    reseller = Reseller(user=user, stripe_customer_ref="cus_test")
    family = ProductFamily(
        name="Fallback family",
        code="fallback-family",
        service_type="http_proxy",
        defaults={},
        constraints={},
        enabled=True,
    )
    product = Product(
        family=family,
        name="Fallback product",
        code="fallback-product",
        overrides={},
        enabled=True,
    )
    db_session.add_all([reseller, product])
    db_session.flush()
    api_key = issue_reseller_api_key(db_session, reseller)
    db_session.add_all(
        [
            ProductPrice(
                product_id=product.id,
                setup_cents=1000,
                monthly_cents=2000,
                currency="USD",
            ),
            ResellerProductAccess(
                reseller_id=reseller.id,
                product_id=product.id,
                allowed=True,
            ),
            StockQuota(
                reseller_id=reseller.id,
                scope_type=StockQuotaScope.PRODUCT,
                scope_id=product.id,
                max_services=1,
            ),
        ]
    )
    db_session.flush()
    CreditLedgerService.credit(db_session, reseller.id, 500)
    PaymentOrchestrator().register_stripe_method(
        db_session, reseller, method_ref="pm_deploy"
    )
    db_session.commit()
    fake_stripe.charge_result = GatewayChargeResult(
        status=GatewayResultStatus.REQUIRES_ACTION,
        external_ref="pi_deploy_action",
        client_secret="pi_deploy_action_secret",
    )

    first = client.post(
        "/api/reseller/bare-metal/services",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": "fallback-deploy-one",
        },
        json={
            "name": "fallback-proxy",
            "external_service_id": "fallback-proxy-1",
            "external_user_id": "fallback-client",
            "service_type": "http_proxy",
            "product_code": product.code,
        },
    )
    assert first.status_code == 402, first.text
    assert first.json()["detail"]["pending_action"] is True

    fake_stripe.event = {
        "id": "evt_deploy_action",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_deploy_action",
                "amount_received": 2500,
                "currency": "usd",
            }
        },
    }
    webhook = client.post(
        "/api/reseller-panel/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "valid"},
    )
    assert webhook.status_code == 200, webhook.text
    response = client.post(
        "/api/reseller/bare-metal/services",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": "fallback-deploy-one",
        },
        json={
            "name": "fallback-proxy",
            "external_service_id": "fallback-proxy-1",
            "external_user_id": "fallback-client",
            "service_type": "http_proxy",
            "product_code": product.code,
        },
    )

    assert response.status_code == 201, response.text
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 0
    invoice = reseller.invoices[0]
    assert invoice.status == InvoiceStatus.PAID
    assert [row.amount_cents for row in invoice.payments] == [500, 2500]


def _paypal_webhook_headers(signature="valid"):
    return {
        "PayPal-Auth-Algo": "SHA256withRSA",
        "PayPal-Cert-Url": "https://api.paypal.com/cert.pem",
        "PayPal-Transmission-Id": "transmission-1",
        "PayPal-Transmission-Sig": signature,
        "PayPal-Transmission-Time": "2026-07-28T18:00:00Z",
    }


def test_paypal_setup_tenant_role_idempotency_and_delete(
    client, db_session, fake_paypal, monkeypatch
):
    monkeypatch.setattr("app.core.config.settings.paypal_client_id", "client")
    monkeypatch.setattr("app.core.config.settings.paypal_client_secret", "secret")
    first, first_headers = _login(client, db_session, "paypal-first")
    _second, second_headers = _login(client, db_session, "paypal-second")
    _plain, plain_headers = _login(
        client, db_session, "paypal-plain", reseller=False
    )

    dashboard = client.get(
        "/api/reseller-panel/dashboard", headers=first_headers
    )
    assert dashboard.json()["paypal_enabled"] is True
    assert (
        client.post(
            "/api/reseller-panel/paypal/setup-token", headers=plain_headers
        ).status_code
        == 403
    )
    setup = client.post(
        "/api/reseller-panel/paypal/setup-token", headers=first_headers
    )
    assert setup.status_code == 200, setup.text
    assert setup.json() == {
        "setup_token_id": f"SETUP-{first.id}",
        "approval_url": f"https://paypal.test/approve/{first.id}",
    }
    stolen = client.post(
        "/api/reseller-panel/paypal/payment-methods",
        headers=second_headers,
        json={"setup_token_id": setup.json()["setup_token_id"]},
    )
    assert stolen.status_code == 403

    saved = client.post(
        "/api/reseller-panel/paypal/payment-methods",
        headers=first_headers,
        json={
            "setup_token_id": setup.json()["setup_token_id"],
            "tier": 3,
            "label": "Primary PayPal",
        },
    )
    replay = client.post(
        "/api/reseller-panel/paypal/payment-methods",
        headers=first_headers,
        json={"setup_token_id": setup.json()["setup_token_id"]},
    )
    assert saved.status_code == 201, saved.text
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == saved.json()["id"]
    assert saved.json()["provider"] == "paypal"
    assert saved.json()["label"] == "Primary PayPal"
    assert "provider_method_ref" not in saved.json()
    deleted = client.delete(
        f"/api/reseller-panel/payment-methods/{saved.json()['id']}",
        headers=first_headers,
    )
    assert deleted.status_code == 204
    assert fake_paypal.detached == [f"VAULT-{first.id}"]


def test_paypal_webhook_accounting_dedupe_and_amount_mismatch(
    client, db_session, fake_paypal
):
    reseller, _headers = _login(client, db_session, "paypal-webhooks")
    topup = InvoiceService.create_topup(
        db_session, reseller_id=reseller.id, amount_cents=2000
    )
    payment = Payment(
        invoice_id=topup.id,
        gateway="paypal",
        status=PaymentStatus.PENDING,
        amount_cents=2000,
        currency="USD",
        external_ref="ORDER-topup",
    )
    db_session.add(payment)
    db_session.commit()

    fake_paypal.normalized_event = NormalizedGatewayWebhookEvent(
        event_id="WH-PAYPAL-SUCCESS",
        event_type="PAYMENT.CAPTURE.COMPLETED",
        action=GatewayWebhookAction.PAYMENT_COMPLETED,
        external_ref="CAPTURE-topup",
        related_external_ref="ORDER-topup",
        amount_cents=2000,
        currency="USD",
    )
    invalid = client.post(
        "/api/reseller-panel/webhooks/paypal",
        content=b"{}",
        headers=_paypal_webhook_headers("invalid"),
    )
    first = client.post(
        "/api/reseller-panel/webhooks/paypal",
        content=b"{}",
        headers=_paypal_webhook_headers(),
    )
    replay = client.post(
        "/api/reseller-panel/webhooks/paypal",
        content=b"{}",
        headers=_paypal_webhook_headers(),
    )
    assert invalid.status_code == 400
    assert first.json() == {"received": True, "duplicate": False}
    assert replay.json() == {"received": True, "duplicate": True}
    db_session.refresh(reseller)
    db_session.refresh(payment)
    assert reseller.cached_balance_cents == 2000
    assert payment.external_ref == "CAPTURE-topup"
    CreditLedgerService.debit(
        db_session,
        reseller.id,
        500,
        description="Spend part of PayPal top-up before refund",
    )
    db_session.commit()

    fake_paypal.normalized_event = NormalizedGatewayWebhookEvent(
        event_id="WH-PAYPAL-REFUND",
        event_type="PAYMENT.CAPTURE.REFUNDED",
        action=GatewayWebhookAction.PAYMENT_REVERSED,
        external_ref="CAPTURE-topup",
    )
    refund = client.post(
        "/api/reseller-panel/webhooks/paypal",
        content=b"{}",
        headers=_paypal_webhook_headers(),
    )
    refund_replay = client.post(
        "/api/reseller-panel/webhooks/paypal",
        content=b"{}",
        headers=_paypal_webhook_headers(),
    )
    assert refund.status_code == 200
    assert refund_replay.json()["duplicate"] is True
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == -500

    deploy = InvoiceService.create_deploy(
        db_session,
        reseller_id=reseller.id,
        amount_cents=700,
        description="Disputed PayPal invoice",
    )
    disputed_payment = Payment(
        invoice_id=deploy.id,
        gateway="paypal",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=700,
        currency="USD",
        external_ref="CAPTURE-dispute",
    )
    db_session.add(disputed_payment)
    db_session.flush()
    PaymentOrchestrator.finalize_success(db_session, disputed_payment)
    db_session.commit()
    fake_paypal.normalized_event = NormalizedGatewayWebhookEvent(
        event_id="WH-PAYPAL-DISPUTE",
        event_type="CUSTOMER.DISPUTE.CREATED",
        action=GatewayWebhookAction.PAYMENT_DISPUTED,
        external_ref="CAPTURE-dispute",
    )
    dispute = client.post(
        "/api/reseller-panel/webhooks/paypal",
        content=b"{}",
        headers=_paypal_webhook_headers(),
    )
    assert dispute.status_code == 200
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == -1200
    assert deploy.status == InvoiceStatus.FAILED

    mismatch_invoice = InvoiceService.create_topup(
        db_session, reseller_id=reseller.id, amount_cents=500
    )
    mismatch_payment = Payment(
        invoice_id=mismatch_invoice.id,
        gateway="paypal",
        status=PaymentStatus.PENDING,
        amount_cents=500,
        currency="USD",
        external_ref="CAPTURE-mismatch",
    )
    db_session.add(mismatch_payment)
    db_session.commit()
    fake_paypal.normalized_event = NormalizedGatewayWebhookEvent(
        event_id="WH-PAYPAL-MISMATCH",
        event_type="PAYMENT.CAPTURE.COMPLETED",
        action=GatewayWebhookAction.PAYMENT_COMPLETED,
        external_ref="CAPTURE-mismatch",
        amount_cents=501,
        currency="USD",
    )
    mismatch = client.post(
        "/api/reseller-panel/webhooks/paypal",
        content=b"{}",
        headers=_paypal_webhook_headers(),
    )
    assert mismatch.status_code == 400
    db_session.refresh(mismatch_payment)
    assert mismatch_payment.status == PaymentStatus.PENDING
    assert (
        db_session.query(GatewayWebhookEvent)
        .filter(GatewayWebhookEvent.event_id == "WH-PAYPAL-MISMATCH")
        .count()
        == 0
    )
