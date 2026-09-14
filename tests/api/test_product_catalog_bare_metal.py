"""bare_metal families/products on the catalog and billing product list."""
from app.dao.billing_integration_dao import BillingIntegrationDAO


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_create_bare_metal_family_product_on_billing_list(
    client, test_admin_user, db_session
):
    headers = _admin_headers(client, test_admin_user)

    family = client.post(
        "/api/product-catalog/families",
        headers=headers,
        json={
            "name": "Dedicated Servers",
            "code": "bm-dedicated",
            "service_type": "bare_metal",
        },
    )
    assert family.status_code == 201, family.text
    family_id = family.json()["id"]

    families = client.get(
        "/api/product-catalog/families",
        headers=headers,
        params={"service_type": "bare_metal"},
    )
    assert families.status_code == 200
    row = next(f for f in families.json() if f["id"] == family_id)
    assert row["service_type"] == "bare_metal"
    assert row["provisioning_backend"] == "server_group"

    product = client.post(
        "/api/product-catalog/products",
        headers=headers,
        json={
            "family_id": family_id,
            "name": "EPYC 32C",
            "code": "bm-epyc-32c",
            "description": "Dedicated EPYC",
        },
    )
    assert product.status_code == 201, product.text

    key = BillingIntegrationDAO.create(
        db_session, name="whmcs-bm-catalog", integration_type="whmcs"
    ).plaintext_api_key

    listed = client.get(
        "/api/billing/products",
        headers={"Authorization": f"Bearer {key}"},
        params={"service_type": "bare_metal"},
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    match = next((p for p in rows if p["code"] == "bm-epyc-32c"), None)
    assert match is not None
    assert match["name"] == "EPYC 32C"
    assert match["service_type"] == "bare_metal"
    assert match["checkout_os_mode"] == "server_group"
    assert "os_profiles" not in match

    detail = client.get(
        "/api/billing/products/bm-epyc-32c",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["family"]["code"] == "bm-dedicated"
    assert "os_profiles" not in detail.json()
    assert detail.json()["checkout_os_mode"] == "server_group"
