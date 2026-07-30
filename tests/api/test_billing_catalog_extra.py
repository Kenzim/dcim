"""Read-only billing catalog endpoints, with filesystem and service mocks."""
from types import SimpleNamespace

from app.dao.billing_integration_dao import BillingIntegrationDAO


def _key(db):
    integration = BillingIntegrationDAO.create(db, name="catalog-extra", integration_type="whmcs")
    return integration.plaintext_api_key


def _headers(key):
    return {"Authorization": f"Bearer {key}"}


def test_billing_catalog_files_and_temp_os(client, db_session, monkeypatch, tmp_path):
    key = _key(db_session)
    iso = tmp_path / "rescue.ISO"
    iso.write_bytes(b"x" * 1024)
    (tmp_path / "ignore.txt").write_text("x")
    monkeypatch.setattr("app.api.billing.os.path.exists", lambda path: True)
    monkeypatch.setattr("app.api.billing.os.listdir", lambda path: ["rescue.ISO", "ignore.txt"])
    monkeypatch.setattr("app.api.billing.os.path.isfile", lambda path: path.endswith("ISO"))
    monkeypatch.setattr("app.api.billing.os.path.getsize", lambda path: 1024 * 1024 * 3)

    isos = client.get("/api/billing/isos", headers=_headers(key))
    assert isos.status_code == 200
    assert isos.json() == [{"id": "rescue.ISO", "name": "rescue.ISO", "size_mb": 3.0}]

    temp = SimpleNamespace(id="debian-live", name="Debian Live", description=None)
    monkeypatch.setattr(
        "app.api.billing.get_temp_os_service",
        lambda: SimpleNamespace(scan_os_configs=lambda: [temp]),
    )
    response = client.get("/api/billing/temp-os", headers=_headers(key))
    assert response.status_code == 200
    assert response.json() == [{"id": "debian-live", "name": "Debian Live", "description": ""}]


def test_billing_catalog_templates_and_empty_iso_dir(client, db_session, monkeypatch):
    key = _key(db_session)
    monkeypatch.setattr("app.api.billing.os.path.exists", lambda path: False)
    assert client.get("/api/billing/isos", headers=_headers(key)).json() == []

    param = SimpleNamespace(type="password", label="Password", required=True, default=None, options=None, help="secret")
    visible = SimpleNamespace(
        id="debian", name="Debian", description="stable", os_type="linux",
        parameters={"admin_password": param}, user_reinstallable=True,
    )
    hidden = SimpleNamespace(id="internal", name="Internal", description="", os_type="linux", parameters={}, user_reinstallable=False)
    monkeypatch.setattr(
        "app.api.billing.get_template_service",
        lambda: SimpleNamespace(get_all_templates=lambda: [visible, hidden]),
    )
    response = client.get("/api/billing/os-templates", headers=_headers(key))
    assert response.status_code == 200
    assert response.json()[0]["id"] == "debian"
    assert response.json()[0]["parameters"]["admin_password"]["required"] is True
