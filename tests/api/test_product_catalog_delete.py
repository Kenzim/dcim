from app.models.service import Service, ServiceStatus, ServiceType, ProvisioningSource


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _create_product(client, headers, code="del-me"):
    response = client.post(
        "/api/product-catalog/products",
        headers=headers,
        json={"name": "Deletable", "code": code},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_delete_product_success(client, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    product_id = _create_product(client, headers)

    response = client.delete(f"/api/product-catalog/products/{product_id}", headers=headers)
    assert response.status_code == 204

    listed = client.get("/api/product-catalog/products", headers=headers)
    assert all(p["id"] != product_id for p in listed.json())


def test_delete_product_not_found(client, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    response = client.delete("/api/product-catalog/products/999999", headers=headers)
    assert response.status_code == 404


def test_delete_product_blocked_when_in_use(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    product_id = _create_product(client, headers, code="in-use-product")

    service = Service(
        name="svc-using-product",
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
        product_code="in-use-product",
    )
    db_session.add(service)
    db_session.commit()

    response = client.delete(f"/api/product-catalog/products/{product_id}", headers=headers)
    assert response.status_code == 409

    # Terminating the service should unblock deletion.
    service.status = ServiceStatus.TERMINATED
    db_session.commit()

    response = client.delete(f"/api/product-catalog/products/{product_id}", headers=headers)
    assert response.status_code == 204
