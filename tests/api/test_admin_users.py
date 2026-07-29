"""
Tests for /api/admin/clients: non-admin users always have full client portal
access (impersonation + billing SSO), independent of whether a password has
been set. Password is a separate, optional login mechanism.
"""
from app.dao.user_dao import UserDAO
from app.models.billing_integration import BillingIntegration


def _login_admin(client) -> str:
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_integration(db_session, name="whmcs-clients"):
    integration = BillingIntegration(
        name=name, integration_type="whmcs", api_key=f"k-{name}", enabled=True, config={}
    )
    db_session.add(integration)
    db_session.commit()
    db_session.refresh(integration)
    return integration


def _make_billing_user(db_session, integration, external_user_id="ext-1", username="billingclient", email="billing@example.com"):
    """A User with a billing identity stamped directly on it (collapsed model)."""
    return UserDAO.create(
        db_session,
        username=username,
        email=email,
        billing_integration_id=integration.id,
        external_user_id=external_user_id,
        external_username="legacyclient",
        external_email="legacy@example.com",
    )


def test_create_client_without_password_has_full_portal_access(client, db_session, test_admin_user):
    admin_token = _login_admin(client)

    resp = client.post(
        "/api/admin/clients",
        headers=_auth(admin_token),
        json={"username": "blankpwclient", "email": "blankpw@example.com"},
    )
    assert resp.status_code == 201, resp.text
    profile = resp.json()
    assert profile["has_password"] is False

    # Password login must be rejected (no password set) ...
    login_resp = client.post(
        "/api/users/login",
        json={"username": "blankpwclient", "password": "anything"},
    )
    assert login_resp.status_code == 401

    # ... but the client always has full portal access via admin impersonation.
    impersonate_resp = client.post(
        f"/api/admin/clients/{profile['user_id']}/impersonate",
        headers=_auth(admin_token),
    )
    assert impersonate_resp.status_code == 200, impersonate_resp.text
    imp_token = impersonate_resp.json()["token"]

    me_resp = client.get("/api/client/me", headers=_auth(imp_token))
    assert me_resp.status_code == 200
    assert me_resp.json()["username"] == "blankpwclient"


def test_create_client_with_password_enables_password_login(client, db_session, test_admin_user):
    admin_token = _login_admin(client)

    resp = client.post(
        "/api/admin/clients",
        headers=_auth(admin_token),
        json={"username": "pwclient", "email": "pw@example.com", "password": "correctPassword123"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["has_password"] is True

    login_resp = client.post(
        "/api/users/login",
        json={"username": "pwclient", "password": "correctPassword123"},
    )
    assert login_resp.status_code == 200


def test_list_clients_shows_billing_identity(client, db_session, test_admin_user):
    """A User with a billing identity stamped on it shows up in the clients
    list with its billing fields populated — there is no separate 'billing
    only' bucket in the collapsed model."""
    integration = _make_integration(db_session)
    user = _make_billing_user(db_session, integration)

    admin_token = _login_admin(client)
    resp = client.get("/api/admin/clients", headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    rows = resp.json()

    matches = [r for r in rows if r["user_id"] == user.id]
    assert len(matches) == 1
    row = matches[0]
    assert row["has_password"] is False
    assert row["username"] == user.username
    assert row["external_user_id"] == user.id
    assert row["integration_id"] == integration.id


def test_get_client_external_profile_resolves_billing_user(client, db_session, test_admin_user):
    integration = _make_integration(db_session)
    user = _make_billing_user(db_session, integration, external_user_id="ext-2", username="billingclient2", email="billing2@example.com")

    admin_token = _login_admin(client)
    resp = client.get(f"/api/admin/clients/external/{user.id}", headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    profile = resp.json()
    assert profile["has_password"] is False
    assert profile["external_user_id"] == user.id
    assert profile["user_id"] == user.id


def test_list_clients_has_password_filter(client, db_session, test_admin_user):
    admin_token = _login_admin(client)
    client.post(
        "/api/admin/clients",
        headers=_auth(admin_token),
        json={"username": "filterpw", "email": "filterpw@example.com", "password": "somePassword123"},
    )
    client.post(
        "/api/admin/clients",
        headers=_auth(admin_token),
        json={"username": "filternopw", "email": "filternopw@example.com"},
    )

    with_pw = client.get("/api/admin/clients", headers=_auth(admin_token), params={"has_password": "true"}).json()
    without_pw = client.get("/api/admin/clients", headers=_auth(admin_token), params={"has_password": "false"}).json()

    assert any(r["username"] == "filterpw" for r in with_pw)
    assert all(r["has_password"] is True for r in with_pw)
    assert any(r["username"] == "filternopw" for r in without_pw)
    assert all(r["has_password"] is False for r in without_pw)


def test_set_and_clear_client_password(client, db_session, test_admin_user):
    admin_token = _login_admin(client)
    created = client.post(
        "/api/admin/clients",
        headers=_auth(admin_token),
        json={"username": "resetpw", "email": "resetpw@example.com"},
    ).json()
    user_id = created["user_id"]
    assert created["has_password"] is False

    set_resp = client.put(
        f"/api/admin/clients/{user_id}/password",
        headers=_auth(admin_token),
        json={"password": "newSecret123"},
    )
    assert set_resp.status_code == 200, set_resp.text

    login_resp = client.post("/api/users/login", json={"username": "resetpw", "password": "newSecret123"})
    assert login_resp.status_code == 200

    clear_resp = client.put(
        f"/api/admin/clients/{user_id}/password",
        headers=_auth(admin_token),
        json={"password": ""},
    )
    assert clear_resp.status_code == 200, clear_resp.text

    login_resp_2 = client.post("/api/users/login", json={"username": "resetpw", "password": "newSecret123"})
    assert login_resp_2.status_code == 401

    profile = client.get(f"/api/admin/clients/{user_id}", headers=_auth(admin_token)).json()
    assert profile["has_password"] is False


def test_cannot_impersonate_admin_account(client, db_session, test_admin_user):
    admin_token = _login_admin(client)
    resp = client.post(
        f"/api/admin/clients/{test_admin_user.id}/impersonate",
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400


def test_cannot_impersonate_reseller_account(client, db_session, test_admin_user):
    from app.models.user import User

    reseller_user = User(username="reselleracct", email="reseller@example.com", is_reseller=True)
    db_session.add(reseller_user)
    db_session.commit()
    db_session.refresh(reseller_user)

    admin_token = _login_admin(client)
    resp = client.post(
        f"/api/admin/clients/{reseller_user.id}/impersonate",
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400
