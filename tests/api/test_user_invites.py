"""Staff and client invitations: creation, links, acceptance and pending state."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.commerce_auth_extra import UserInviteToken
from app.models.commerce_email import EmailMessage
from app.models.user import User


@pytest.fixture
def public_url(monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    monkeypatch.setattr(settings, "commerce_public_app_url", None)


def _login(client, username="testuser", password="testpassword123"):
    return client.post("/api/users/login", json={"username": username, "password": password})


@pytest.fixture
def admin_headers(client, test_admin_user):
    token = _login(client, "admin", "adminpassword123").json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _invite(client, headers, **body):
    body.setdefault("email", "new@example.com")
    body.setdefault("kind", "client")
    return client.post("/api/admin/invites", json=body, headers=headers)


def _token_for(db_session, email):
    row = (
        db_session.execute(select(EmailMessage).where(EmailMessage.to_address == email).order_by(EmailMessage.id))
        .scalars()
        .all()[-1]
    )
    token = row.body_text.split("token=")[1].split()[0]
    # End the read transaction so the app's own session can write (SQLite locks).
    db_session.rollback()
    return token


def _accept(client, token, username="chosenname", password="freshpassword99"):
    return client.post(
        "/api/users/invite/accept", json={"token": token, "username": username, "password": password}
    )


def test_invite_without_username_creates_placeholder(client, admin_headers, db_session, public_url):
    resp = _invite(client, admin_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"].startswith("invite-")
    user = db_session.get(User, body["user_id"])
    assert user.password is None and not user.is_admin


def test_placeholder_usernames_are_unique(client, admin_headers, public_url):
    a = _invite(client, admin_headers, email="a@example.com").json()["username"]
    b = _invite(client, admin_headers, email="b@example.com").json()["username"]
    assert a != b


def test_invite_with_username_prefills_lookup(client, admin_headers, db_session, public_url):
    resp = _invite(client, admin_headers, username="alice", kind="staff")
    assert resp.status_code == 201
    assert db_session.get(User, resp.json()["user_id"]).is_admin is True
    info = client.post("/api/users/invite/lookup", json={"token": _token_for(db_session, "new@example.com")})
    assert info.json() == {"email": "new@example.com", "username": "alice", "kind": "staff"}


def test_lookup_hides_placeholder_username(client, admin_headers, db_session, public_url):
    _invite(client, admin_headers)
    info = client.post("/api/users/invite/lookup", json={"token": _token_for(db_session, "new@example.com")})
    assert info.json()["username"] == ""


def test_invite_rejects_duplicates_and_bad_input(client, admin_headers, test_user, public_url):
    assert _invite(client, admin_headers, email=test_user.email).status_code == 409
    assert _invite(client, admin_headers, username=test_user.username).status_code == 409
    assert _invite(client, admin_headers, kind="staff", permission_set_id=1).status_code == 400
    assert _invite(client, admin_headers, permission_set_id=99999).status_code == 404
    assert _invite(client, admin_headers, kind="other").status_code == 422


def test_invite_requires_admin(client, test_user, public_url):
    token = _login(client).json()["token"]
    resp = _invite(client, {"Authorization": f"Bearer {token}"})
    assert resp.status_code in (401, 403)
    assert client.post("/api/admin/invites", json={"email": "x@example.com", "kind": "client"}).status_code in (401, 403)


def test_invite_email_contains_link(client, admin_headers, db_session, public_url):
    _invite(client, admin_headers)
    row = db_session.execute(select(EmailMessage).where(EmailMessage.to_address == "new@example.com")).scalar_one()
    assert row.event == "user_invite"
    assert "https://rackflow.test/accept-invite?token=" in row.body_text
    assert "https://rackflow.test/accept-invite?token=" in row.body_html
    assert "Accept invitation" in row.body_html


def test_invite_needs_base_url(client, admin_headers, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", None)
    monkeypatch.setattr(settings, "commerce_public_app_url", None)
    assert _invite(client, admin_headers).status_code == 503
    assert db_session.execute(select(User).where(User.email == "new@example.com")).first() is None


def test_token_is_stored_hashed(client, admin_headers, db_session, public_url):
    _invite(client, admin_headers)
    token = _token_for(db_session, "new@example.com")
    row = db_session.execute(select(UserInviteToken)).scalar_one()
    assert token not in row.token_hash
    assert row.invited_by_user_id is not None
    delta = row.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)


def test_login_refused_before_acceptance_and_allowed_after(client, admin_headers, db_session, public_url):
    _invite(client, admin_headers)
    token = _token_for(db_session, "new@example.com")
    pending = db_session.execute(select(User).where(User.email == "new@example.com")).scalar_one()
    assert _login(client, pending.username, "anything12345").status_code == 401

    resp = _accept(client, token)
    assert resp.status_code == 200
    assert _login(client, "chosenname", "freshpassword99").status_code == 200
    db_session.expire_all()
    assert db_session.get(User, pending.id).username == "chosenname"


def test_acceptance_is_single_use(client, admin_headers, db_session, public_url):
    _invite(client, admin_headers)
    token = _token_for(db_session, "new@example.com")
    assert _accept(client, token).status_code == 200
    assert _accept(client, token, username="another").status_code == 400
    assert client.post("/api/users/invite/lookup", json={"token": token}).status_code == 400


def test_expired_token_is_rejected(client, admin_headers, db_session, public_url):
    _invite(client, admin_headers)
    token = _token_for(db_session, "new@example.com")
    row = db_session.execute(select(UserInviteToken)).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    assert _accept(client, token).status_code == 400
    assert client.post("/api/users/invite/lookup", json={"token": token}).status_code == 400


def test_accept_validates_username_and_password(client, admin_headers, test_user, db_session, public_url):
    _invite(client, admin_headers)
    token = _token_for(db_session, "new@example.com")
    assert _accept(client, token, username=test_user.username).status_code == 409
    assert _accept(client, token, username="ab").status_code == 400
    assert _accept(client, token, username="has space").status_code == 400
    assert _accept(client, token, password="short").status_code == 422
    assert _accept(client, token, password="x" * 80).status_code == 422
    # None of the failures consumed the token.
    assert _accept(client, token).status_code == 200


def test_invitee_may_keep_prefilled_username(client, admin_headers, db_session, public_url):
    _invite(client, admin_headers, username="alice")
    token = _token_for(db_session, "new@example.com")
    assert _accept(client, token, username="alice").status_code == 200
    assert _login(client, "alice", "freshpassword99").status_code == 200


def test_resend_retires_old_token(client, admin_headers, db_session, public_url):
    user_id = _invite(client, admin_headers).json()["user_id"]
    first = _token_for(db_session, "new@example.com")
    assert client.post(f"/api/admin/invites/{user_id}/resend", headers=admin_headers).status_code == 200
    second = _token_for(db_session, "new@example.com")
    assert first != second
    assert _accept(client, first).status_code == 400
    assert _accept(client, second).status_code == 200


def test_revoke_kills_token_but_keeps_account(client, admin_headers, db_session, public_url):
    user_id = _invite(client, admin_headers).json()["user_id"]
    token = _token_for(db_session, "new@example.com")
    assert client.delete(f"/api/admin/invites/{user_id}", headers=admin_headers).status_code == 200
    assert _accept(client, token).status_code == 400
    assert db_session.get(User, user_id) is not None
    # Still pending, so it can be invited again.
    assert client.post(f"/api/admin/invites/{user_id}/resend", headers=admin_headers).status_code == 200
    assert _accept(client, _token_for(db_session, "new@example.com")).status_code == 200


def test_resend_and_revoke_refuse_active_users(client, admin_headers, test_user, public_url):
    assert client.post(f"/api/admin/invites/{test_user.id}/resend", headers=admin_headers).status_code == 400
    assert client.delete(f"/api/admin/invites/{test_user.id}", headers=admin_headers).status_code == 400
    assert client.post("/api/admin/invites/999999/resend", headers=admin_headers).status_code == 404


def test_lists_show_invited_until_accepted(client, admin_headers, db_session, public_url):
    _invite(client, admin_headers, email="c@example.com")
    _invite(client, admin_headers, email="s@example.com", kind="staff")

    clients = client.get("/api/admin/clients", headers=admin_headers).json()
    admins = client.get("/api/admin/admins", headers=admin_headers).json()
    assert [c["invited"] for c in clients if c["email"] == "c@example.com"] == [True]
    assert [a["invited"] for a in admins if a["email"] == "s@example.com"] == [True]
    assert not any(a["invited"] for a in admins if a["email"] != "s@example.com")

    assert _accept(client, _token_for(db_session, "c@example.com")).status_code == 200
    clients = client.get("/api/admin/clients", headers=admin_headers).json()
    assert [c["invited"] for c in clients if c["email"] == "c@example.com"] == [False]


def test_pending_client_can_still_be_impersonated(client, admin_headers, public_url):
    user_id = _invite(client, admin_headers).json()["user_id"]
    resp = client.post(f"/api/admin/clients/{user_id}/impersonate", headers=admin_headers)
    assert resp.status_code == 200


def test_acceptance_revokes_existing_sessions(client, admin_headers, db_session, public_url):
    user_id = _invite(client, admin_headers).json()["user_id"]
    token = _token_for(db_session, "new@example.com")
    session = client.post(f"/api/admin/clients/{user_id}/impersonate", headers=admin_headers).json()["token"]
    headers = {"Authorization": f"Bearer {session}"}
    assert client.get("/api/users/me", headers=headers).status_code == 200
    _accept(client, token)
    assert client.get("/api/users/me", headers=headers).status_code == 401


def test_invite_actions_are_audited(client, admin_headers, db_session, public_url):
    from app.models.commerce_audit import UserAuditEvent

    user_id = _invite(client, admin_headers).json()["user_id"]
    client.post(f"/api/admin/invites/{user_id}/resend", headers=admin_headers)
    client.delete(f"/api/admin/invites/{user_id}", headers=admin_headers)
    client.post(f"/api/admin/invites/{user_id}/resend", headers=admin_headers)
    _accept(client, _token_for(db_session, "new@example.com"))
    actions = list(
        db_session.execute(select(UserAuditEvent.action).where(UserAuditEvent.subject_user_id == user_id)).scalars()
    )
    assert actions.count("user_invite_sent") == 3
    assert "user_invite_revoked" in actions
    assert "user_invite_accepted" in actions
