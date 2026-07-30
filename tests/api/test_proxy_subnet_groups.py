"""Proxy subnet groups (IPAM pools) + catalog defaults that allocate from them."""
from app.dao.ipam_dao import IPAMDAO
from app.dao.location_dao import LocationDAO
from app.dao.proxy_subnet_group_dao import ProxySubnetGroupDAO
from app.models.ipam import IPAddress


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_proxy_subnet_group_crud_and_member_validation(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-proxy-pool")
    subnet_a = IPAMDAO.create_subnet(db_session, name="pool-a", cidr="198.51.100.0/29", location_id=location.id)
    subnet_b = IPAMDAO.create_subnet(db_session, name="pool-b", cidr="198.51.100.8/29", location_id=location.id)

    created = client.post(
        "/api/admin/proxy-subnet-groups",
        headers=headers,
        json={
            "name": "Kenzi Home",
            "code": "kenzi-home",
            "subnet_ids": [subnet_a.id, subnet_b.id],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["code"] == "kenzi-home"
    assert sorted(body["subnet_ids"]) == sorted([subnet_a.id, subnet_b.id])
    assert len(body["members"]) == 2

    listed = client.get("/api/admin/proxy-subnet-groups", headers=headers)
    assert listed.status_code == 200
    assert any(row["id"] == body["id"] for row in listed.json())

    bad = client.post(
        "/api/admin/proxy-subnet-groups",
        headers=headers,
        json={"name": "Bad pool", "subnet_ids": [999999]},
    )
    assert bad.status_code == 400, bad.text

    patched = client.patch(
        f"/api/admin/proxy-subnet-groups/{body['id']}",
        headers=headers,
        json={"subnet_ids": [subnet_a.id], "enabled": True},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["subnet_ids"] == [subnet_a.id]

    deleted = client.delete(f"/api/admin/proxy-subnet-groups/{body['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    assert ProxySubnetGroupDAO.get_by_id(db_session, body["id"]) is None


def test_assign_ip_respects_subnet_ids_pool(db_session):
    location = LocationDAO.create(db_session, name="loc-assign-pool")
    in_pool = IPAMDAO.create_subnet(db_session, name="in-pool", cidr="203.0.113.0/30", location_id=location.id)
    out_pool = IPAMDAO.create_subnet(db_session, name="out-pool", cidr="203.0.113.4/30", location_id=location.id)

    from app.dao.service_dao import ServiceDAO
    from app.models.service import ServiceStatus, ServiceType, ProvisioningSource

    service = ServiceDAO.create_bare_metal(
        db_session,
        name="svc-pool-assign",
        server_id=None,
        owner_user_id=None,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.PENDING,
        provisioning_source=ProvisioningSource.INTERNAL,
    )

    # Exhaust the in-pool subnet so the only free IPs are outside the pool.
    in_pool_ips = (
        db_session.query(IPAddress).filter(IPAddress.subnet_id == in_pool.id, IPAddress.state == "free").all()
    )
    for ip in in_pool_ips:
        ip.state = "reserved"
    db_session.commit()

    try:
        IPAMDAO.assign_ip(db_session, service_id=service.id, subnet_ids=[in_pool.id])
        assert False, "expected no free IP inside pool"
    except ValueError as exc:
        assert "No free IP" in str(exc) or "No enabled" in str(exc)

    # Without subnet_ids, assign_ip can still use the out-of-pool subnet.
    assignment = IPAMDAO.assign_ip(db_session, service_id=service.id, subnet_ids=[out_pool.id])
    assert assignment.ip.subnet_id == out_pool.id


def test_product_subnet_group_id_drives_admin_create(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    location = LocationDAO.create(db_session, name="loc-group-product")
    pool_subnet = IPAMDAO.create_subnet(
        db_session, name="group-product-pool", cidr="203.0.113.64/29", location_id=location.id
    )
    other_subnet = IPAMDAO.create_subnet(
        db_session, name="group-product-other", cidr="203.0.113.72/29", location_id=location.id
    )

    group = ProxySubnetGroupDAO.create(
        db_session,
        name="Product Pool",
        code="product-pool",
        subnet_ids=[pool_subnet.id],
    )

    family = client.post(
        "/api/product-catalog/families",
        headers=headers,
        json={
            "name": "Group Proxy Family",
            "code": "group-proxy-family",
            "service_type": "http_proxy",
            "defaults": {"ip_count": 2, "subnet_group_id": group.id},
        },
    )
    assert family.status_code == 201, family.text

    product = client.post(
        "/api/product-catalog/products",
        headers=headers,
        json={
            "family_id": family.json()["id"],
            "name": "Group Proxy Product",
            "code": "group-proxy-product",
            "overrides": {},
        },
    )
    assert product.status_code == 201, product.text

    # Reject unknown subnet_group_id on catalog defaults.
    bad = client.post(
        "/api/product-catalog/families",
        headers=headers,
        json={
            "name": "Bad Group Family",
            "code": "bad-group-family",
            "service_type": "http_proxy",
            "defaults": {"subnet_group_id": 999999},
        },
    )
    assert bad.status_code == 400, bad.text

    resp = client.post(
        "/api/admin/services/http-proxy",
        headers=headers,
        json={"name": "svc-from-group-product", "product_code": "group-proxy-product"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert len(body["proxy_assignments"]) == 2

    for assignment in body["proxy_assignments"]:
        ip_row = (
            db_session.query(IPAddress)
            .filter(IPAddress.ip_address == assignment["ip_address"])
            .first()
        )
        assert ip_row is not None
        assert ip_row.subnet_id == pool_subnet.id
        assert ip_row.subnet_id != other_subnet.id


def test_catalog_rejects_missing_subnet_group_on_product_override(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    family = client.post(
        "/api/product-catalog/families",
        headers=headers,
        json={
            "name": "Override Check Family",
            "code": "override-check-family",
            "service_type": "http_proxy",
            "defaults": {"ip_count": 1},
        },
    )
    assert family.status_code == 201, family.text
    product = client.post(
        "/api/product-catalog/products",
        headers=headers,
        json={
            "family_id": family.json()["id"],
            "name": "Override Check Product",
            "code": "override-check-product",
            "overrides": {"subnet_group_id": 424242},
        },
    )
    assert product.status_code == 400, product.text
