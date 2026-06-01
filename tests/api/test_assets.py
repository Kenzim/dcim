from app.models.location import Location
from app.models.server import Server


def _admin_auth_headers(client, test_admin_user):
    login = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert login.status_code == 200
    token = login.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _create_server_with_preview(db_session, preview_asset_id: int) -> Server:
    location = Location(name="Asset API Location", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name="asset-api-server",
        server_ip="172.16.1.10",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"host": "172.16.1.10"},
        preview_asset_id=preview_asset_id,
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_list_labels_and_invalid_label_filter(client):
    labels = client.get("/api/assets/labels")
    assert labels.status_code == 200
    label_values = {row["value"] for row in labels.json()}
    assert "generic" in label_values

    bad = client.get("/api/assets?label=does-not-exist")
    assert bad.status_code == 400
    assert "Invalid label" in bad.json()["detail"]


def test_upload_requires_admin(client):
    response = client.post(
        "/api/assets",
        files={"file": ("logo.png", b"png-bytes", "image/png")},
        data={"label": "generic"},
    )
    assert response.status_code == 401


def test_upload_get_serve_and_delete_asset(client, db_session, test_admin_user):
    headers = _admin_auth_headers(client, test_admin_user)
    payload = b"\x89PNG\r\n\x1a\n\x00test-image"

    upload = client.post(
        "/api/assets",
        files={"file": ("logo.png", payload, "image/png")},
        data={"label": "generic", "description": "Rack image"},
        headers=headers,
    )
    assert upload.status_code == 201
    uploaded = upload.json()
    asset_id = uploaded["id"]
    assert uploaded["filename"] == "logo.png"
    assert uploaded["description"] == "Rack image"

    listed = client.get("/api/assets?label=generic")
    assert listed.status_code == 200
    assert any(row["id"] == asset_id for row in listed.json())

    fetched = client.get(f"/api/assets/{asset_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == asset_id

    served = client.get(f"/api/assets/{asset_id}/file")
    assert served.status_code == 200
    assert served.content == payload
    assert served.headers["content-type"] == "image/png"

    server = _create_server_with_preview(db_session, asset_id)
    deleted = client.delete(f"/api/assets/{asset_id}", headers=headers)
    assert deleted.status_code == 204

    db_session.refresh(server)
    assert server.preview_asset_id is None
    missing = client.get(f"/api/assets/{asset_id}")
    assert missing.status_code == 404


def test_upload_rejects_bad_extension_and_too_large_file(client, test_admin_user):
    headers = _admin_auth_headers(client, test_admin_user)

    bad_ext = client.post(
        "/api/assets",
        files={"file": ("readme.txt", b"hello", "text/plain")},
        data={"label": "generic"},
        headers=headers,
    )
    assert bad_ext.status_code == 400
    assert "Allowed types" in bad_ext.json()["detail"]

    too_large = client.post(
        "/api/assets",
        files={"file": ("huge.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")},
        data={"label": "generic"},
        headers=headers,
    )
    assert too_large.status_code == 400
    assert "File too large" in too_large.json()["detail"]

