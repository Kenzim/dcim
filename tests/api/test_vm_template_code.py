"""API tests for immutable VMTemplate.code."""


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_vm_template_code_required_unique_and_immutable(client, db_session, test_admin_user):
    headers = _admin_headers(client, test_admin_user)

    created = client.post(
        "/api/product-catalog/vm-templates",
        headers=headers,
        json={
            "code": "debian-13",
            "name": "Debian 13",
            "os_type": "Linux - Cloudinit",
            "proxmox_template_name": "debian-13-cloud",
        },
    )
    assert created.status_code == 201, created.text
    tmpl_id = created.json()["id"]

    listed = client.get("/api/product-catalog/vm-templates", headers=headers)
    assert listed.status_code == 200
    row = next(r for r in listed.json() if r["id"] == tmpl_id)
    assert row["code"] == "debian-13"

    conflict = client.post(
        "/api/product-catalog/vm-templates",
        headers=headers,
        json={
            "code": "debian-13",
            "name": "Debian 13 dup",
            "os_type": "Linux - Cloudinit",
            "proxmox_template_name": "debian-13-cloud-2",
        },
    )
    assert conflict.status_code == 409

    updated = client.put(
        f"/api/product-catalog/vm-templates/{tmpl_id}",
        headers=headers,
        json={"name": "Debian 13 renamed", "code": "hacked"},
    )
    assert updated.status_code == 200, updated.text
    listed2 = client.get("/api/product-catalog/vm-templates", headers=headers)
    row2 = next(r for r in listed2.json() if r["id"] == tmpl_id)
    assert row2["code"] == "debian-13"
    assert row2["name"] == "Debian 13 renamed"

    bad = client.post(
        "/api/product-catalog/vm-templates",
        headers=headers,
        json={
            "code": "Bad_Code",
            "name": "Bad",
            "os_type": "Linux - Cloudinit",
            "proxmox_template_name": "bad-code-tpl",
        },
    )
    assert bad.status_code == 400
