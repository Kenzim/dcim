"""http_proxy families/products in the catalog (catalog-create) and the
dedicated admin service-create endpoint that auto-assigns IPs without a
rack Server.
"""
from app.dao.ipam_dao import IPAMDAO
from app.dao.location_dao import LocationDAO


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _proxy_family_and_product(client, headers, *, subnet_id, ip_count=2, code_suffix="1"):
    family = client.post(
        "/api/product-catalog/families",
        headers=headers,
        json={
            "name": f"Proxy Family {code_suffix}",
            "code": f"proxy-family-{code_suffix}",
            "service_type": "http_proxy",
            "defaults": {"ip_count": ip_count, "subnet_id": subnet_id, "allocation_strategy": "first_free"},
        },
    )
    assert family.status_code == 201, family.text
    family_id = family.json()["id"]

    product = client.post(
        "/api/product-catalog/products",
        headers=headers,
        json={
            "family_id": family_id,
            "name": f"Proxy Product {code_suffix}",
            "code": f"proxy-product-{code_suffix}",
            "overrides": {},
        },
    )
    assert product.status_code == 201, product.text
    return family_id, product.json()["id"]


def test_create_http_proxy_family_and_product(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-catalog-proxy")
    subnet = IPAMDAO.create_subnet(db_session, name="catalog-subnet", cidr="203.0.113.0/29", location_id=location.id)

    family_id, product_id = _proxy_family_and_product(client, headers, subnet_id=subnet.id, code_suffix="a")
    assert family_id and product_id

    families = client.get("/api/product-catalog/families", headers=headers)
    assert families.status_code == 200
    assert any(f["id"] == family_id and f["service_type"] == "http_proxy" for f in families.json())


def test_create_http_proxy_family_rejects_bad_ip_count(client, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    resp = client.post(
        "/api/product-catalog/families",
        headers=headers,
        json={
            "name": "Bad Proxy Family",
            "code": "bad-proxy-family",
            "service_type": "http_proxy",
            "defaults": {"ip_count": 999},
        },
    )
    assert resp.status_code == 400, resp.text


def test_admin_create_http_proxy_service_auto_assigns_ips_from_product(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-admin-proxy-create")
    subnet = IPAMDAO.create_subnet(db_session, name="admin-create-subnet", cidr="203.0.113.8/29", location_id=location.id)
    _family_id, _product_id = _proxy_family_and_product(
        client, headers, subnet_id=subnet.id, ip_count=2, code_suffix="b"
    )

    resp = client.post(
        "/api/admin/services/http-proxy",
        headers=headers,
        json={"name": "admin-proxy-svc-1", "product_code": "proxy-product-b"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["service_type"] == "http_proxy"

    assignments = client.get(f"/api/ipam/assignments", headers=headers)
    assert assignments.status_code == 200
    matching = [a for a in assignments.json() if a["service_name"] == "admin-proxy-svc-1"]
    assert len(matching) == 2


def test_admin_create_http_proxy_service_without_product_defaults_to_one_ip(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-admin-proxy-bare")
    IPAMDAO.create_subnet(db_session, name="admin-bare-subnet", cidr="203.0.113.16/29", location_id=location.id)

    resp = client.post(
        "/api/admin/services/http-proxy",
        headers=headers,
        json={"name": "admin-proxy-svc-bare"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["proxy_assignments"] and len(body["proxy_assignments"]) == 1
    assert body["proxy_assignments"][0]["http_url"].startswith("http://")
    assert body["proxy_assignments"][0]["socks5_url"].startswith("socks5://")


def test_admin_create_http_proxy_service_stays_pending_when_pool_exhausted(client, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    # No subnets created at all — pool is empty.
    resp = client.post(
        "/api/admin/services/http-proxy",
        headers=headers,
        json={"name": "admin-proxy-svc-exhausted"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert not body.get("proxy_assignments")


def test_admin_create_http_proxy_service_duplicate_name_rejected(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-admin-proxy-dup")
    IPAMDAO.create_subnet(db_session, name="admin-dup-subnet", cidr="203.0.113.24/29", location_id=location.id)

    first = client.post(
        "/api/admin/services/http-proxy",
        headers=headers,
        json={"name": "admin-proxy-svc-dup"},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/admin/services/http-proxy",
        headers=headers,
        json={"name": "admin-proxy-svc-dup"},
    )
    assert second.status_code == 400
