"""Billing catalog product list/detail for WHMCS Module Settings."""
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.product_catalog_dao import (
    ProductDAO,
    ProductFamilyDAO,
    VMTemplateDAO,
)
from app.dao.vm_config_dao import FamilyVMConfigDAO, ProductVMConfigDAO


def _key(db_session):
    integration = BillingIntegrationDAO.create(
        db_session, name="whmcs-products", integration_type="whmcs"
    )
    return integration.plaintext_api_key


def _seed_vm_product(db_session):
    family = ProductFamilyDAO.create(
        db_session,
        name="VM Family",
        description="vm family",
        code="vm-family",
        service_type="vm",
        provisioning_backend="proxmox",
        defaults={"cpu_cores": 2},
        constraints={},
    )
    FamilyVMConfigDAO.upsert(db_session, family, {"cpu_cores": 2, "ram_mb": 4096})
    product = ProductDAO.create(
        db_session,
        family_id=family.id,
        name="MacOS Test",
        description="Test VM product",
        code="macos-test",
        overrides={},
    )
    ProductVMConfigDAO.upsert(
        db_session, product, extends_family=True, config={"ram_mb": 8192}
    )
    tmpl = VMTemplateDAO.create(
        db_session,
        code="macos-tahoe",
        name="Tahoe Template",
        os_type="macOS",
        proxmox_template_name="tahoe-tmpl",
        description="tmpl",
    )
    ProductDAO.set_vm_templates(db_session, product, [tmpl.id])
    db_session.commit()
    return product, tmpl


def test_list_products_billing_filters_and_preview(client, db_session):
    key = _key(db_session)
    product, tmpl = _seed_vm_product(db_session)
    ProductFamilyDAO.create(
        db_session,
        name="BM Family",
        description=None,
        code="bm-family",
        service_type="bare_metal",
        defaults={},
        constraints={},
    )
    ProductDAO.create(
        db_session,
        family_id=None,
        name="Orphan",
        description=None,
        code="orphan-prod",
        enabled=True,
    )

    resp = client.get(
        "/api/billing/products",
        headers={"Authorization": f"Bearer {key}"},
        params={"service_type": "vm"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["code"] == product.code
    assert row["name"] == "MacOS Test"
    assert row["service_type"] == "vm"
    assert row["effective_specs"]["ram_mb"] == 8192
    assert row["effective_specs"]["cpu_cores"] == 2
    assert row["checkout_os_mode"] == "vm_template"
    assert "os_profiles" not in row
    assert any(t["id"] == tmpl.id for t in row["vm_templates"])


def test_get_product_billing_by_code(client, db_session):
    key = _key(db_session)
    product, _ = _seed_vm_product(db_session)

    resp = client.get(
        f"/api/billing/products/{product.code}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == "macos-test"
    assert body["family"]["code"] == "vm-family"

    missing = client.get(
        "/api/billing/products/does-not-exist",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert missing.status_code == 404
