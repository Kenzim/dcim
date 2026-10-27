"""Account security: forced 2FA, staff password reset, email delivery, admin guards, audit."""

from types import SimpleNamespace

import pyotp
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import settings
from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_auth_extra import PasswordResetToken
from app.models.commerce_email import EmailMessage, EmailMessageStatus
from app.services import email_sender
from app.services.user_session_service import mint_user_session


@pytest.fixture
def encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "service_instance_encryption_key", Fernet.generate_key().decode())
    return settings.service_instance_encryption_key


@pytest.fixture
def public_url(monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    monkeypatch.setattr(settings, "commerce_public_app_url", None)


def _login(client, username="testuser", password="testpassword123"):
    return client.post("/api/users/login", json={"username": username, "password": password})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _audit_actions(db_session, subject_id):
    rows = db_session.execute(
        select(UserAuditEvent.action).where(UserAuditEvent.subject_user_id == subject_id)
    ).scalars()
    return list(rows)


# --- Email delivery -------------------------------------------------------


def _smtp_config(**overrides):
    base = dict(
        smtp_enabled=True,
        smtp_host="smtp.test",
        smtp_port=587,
        smtp_from="noreply@rackflow.test",
        smtp_starttls=False,
        smtp_timeout_seconds=5,
        smtp_username=None,
        smtp_password=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _queue_email(db_session, to="a@example.com", key="k1"):
    row = EmailMessage(
        to_address=to,
        subject="Reset",
        body_text="link",
        body_html=None,
        status=EmailMessageStatus.QUEUED,
        idempotency_key=key,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_queued_email_is_sent_and_marked(db_session, monkeypatch):
    sent = []
    monkeypatch.setattr(email_sender, "deliver", lambda config, **kw: sent.append(kw))
    row = _queue_email(db_session)

    count = email_sender.send_queued_batch(db_session, config=_smtp_config())

    assert count == 1
    assert sent[0]["to_address"] == "a@example.com"
    db_session.refresh(row)
    assert row.status == EmailMessageStatus.SENT
    assert row.sent_at is not None
    assert row.attempts == 1


def test_failed_email_retries_then_gives_up(db_session, monkeypatch):
    def boom(config, **kw):
        raise OSError("smtp down")

    monkeypatch.setattr(email_sender, "deliver", boom)
    row = _queue_email(db_session)
    config = _smtp_config()

    email_sender.send_queued_batch(db_session, config=config)
    db_session.refresh(row)
    assert row.status == EmailMessageStatus.QUEUED
    assert "smtp down" in row.error

    for _ in range(email_sender.MAX_ATTEMPTS - 1):
        email_sender.send_queued_batch(db_session, config=config)
    db_session.refresh(row)
    assert row.status == EmailMessageStatus.FAILED
    assert row.attempts == email_sender.MAX_ATTEMPTS


def test_email_stays_queued_while_smtp_disabled(db_session, monkeypatch):
    monkeypatch.setattr(email_sender, "deliver", lambda *a, **kw: pytest.fail("should not send"))
    row = _queue_email(db_session)

    assert email_sender.send_queued_batch(db_session, config=_smtp_config(smtp_enabled=False)) == 0
    db_session.refresh(row)
    assert row.status == EmailMessageStatus.QUEUED


# --- Staff password reset --------------------------------------------------


def test_reset_request_queues_link_for_staff_account(client, test_admin_user, db_session, public_url):
    resp = client.post("/api/users/password-reset/request", json={"email": test_admin_user.email})
    assert resp.status_code == 200

    row = db_session.execute(select(EmailMessage).where(EmailMessage.to_address == test_admin_user.email)).scalar_one()
    assert row.event == "password_reset"
    assert "https://rackflow.test/reset-password?token=" in row.body_text


def test_reset_request_is_identical_for_unknown_email(client, db_session, public_url):
    resp = client.post("/api/users/password-reset/request", json={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert db_session.execute(select(EmailMessage)).first() is None


def test_reset_without_base_url_issues_no_token(client, test_user, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", None)
    monkeypatch.setattr(settings, "commerce_public_app_url", None)

    resp = client.post("/api/users/password-reset/request", json={"email": test_user.email})
    assert resp.status_code == 200
    assert db_session.execute(select(PasswordResetToken)).first() is None


def _issue_token(client, db_session, email, public_url):
    client.post("/api/users/password-reset/request", json={"email": email})
    row = db_session.execute(select(EmailMessage).where(EmailMessage.to_address == email)).scalars().all()[-1]
    return row.body_text.split("token=")[1].split()[0]


def test_reset_confirm_sets_password_and_revokes_sessions(client, test_user, db_session, public_url):
    old_session = _login(client).json()["token"]
    token = _issue_token(client, db_session, test_user.email, public_url)

    resp = client.post(
        "/api/users/password-reset/confirm",
        json={"token": token, "password": "freshpassword99"},
    )
    assert resp.status_code == 200
    assert client.get("/api/users/me", headers=_bearer(old_session)).status_code == 401
    assert _login(client, password="freshpassword99").status_code == 200
    assert _login(client, password="testpassword123").status_code == 401


def test_reset_token_is_single_use(client, test_user, db_session, public_url):
    token = _issue_token(client, db_session, test_user.email, public_url)
    body = {"token": token, "password": "freshpassword99"}
    assert client.post("/api/users/password-reset/confirm", json=body).status_code == 200
    assert client.post("/api/users/password-reset/confirm", json=body).status_code == 400


def test_newer_reset_request_retires_older_link(client, test_user, db_session, public_url):
    first = _issue_token(client, db_session, test_user.email, public_url)
    _issue_token(client, db_session, test_user.email, public_url)

    resp = client.post(
        "/api/users/password-reset/confirm",
        json={"token": first, "password": "freshpassword99"},
    )
    assert resp.status_code == 400


def test_reset_rejects_password_over_72_bytes(client, test_user, db_session, public_url):
    token = _issue_token(client, db_session, test_user.email, public_url)
    resp = client.post(
        "/api/users/password-reset/confirm",
        json={"token": token, "password": "x" * 80},
    )
    assert resp.status_code == 422


# --- Forced 2FA ------------------------------------------------------------


def _force_2fa(client, admin_token, user_id, required=True):
    return client.put(
        f"/api/admin/clients/{user_id}/require-2fa",
        json={"require_2fa": required},
        headers=_bearer(admin_token),
    )


def test_forced_2fa_restricts_login_until_enrolled(client, test_user, test_admin_user, encryption_key, db_session):
    admin_token = _login(client, "admin", "adminpassword123").json()["token"]
    assert _force_2fa(client, admin_token, test_user.id).status_code == 200

    resp = _login(client)
    body = resp.json()
    assert body["requires_2fa_enrollment"] is True
    assert body["token"]
    token = body["token"]

    # Everything outside the enrolment allowlist is refused with a structured code.
    blocked = client.get("/api/users/sessions", headers=_bearer(token))
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "2fa_enrollment_required"

    assert client.get("/api/users/me", headers=_bearer(token)).status_code == 200
    status = client.get("/api/users/2fa/status", headers=_bearer(token)).json()
    assert status["enrollment_required"] is True
    assert status["enabled"] is False


def test_enrolling_lifts_restriction_on_every_session(client, test_user, test_admin_user, encryption_key, db_session):
    admin_token = _login(client, "admin", "adminpassword123").json()["token"]
    _force_2fa(client, admin_token, test_user.id)

    restricted = _login(client).json()["token"]
    # Mandate set before this session existed, so it's restricted too.
    other = mint_user_session(test_user)
    from app.services.user_session_service import restrict_sessions_for_2fa_enrollment

    restrict_sessions_for_2fa_enrollment(test_user.id)

    setup = client.post(
        "/api/users/2fa/setup", json={"password": "testpassword123"}, headers=_bearer(restricted)
    ).json()
    code = pyotp.TOTP(setup["secret"]).now()
    confirm = client.post("/api/users/2fa/confirm", json={"code": code}, headers=_bearer(restricted))
    assert confirm.status_code == 200
    assert len(confirm.json()["recovery_codes"]) == 10

    assert client.get("/api/users/sessions", headers=_bearer(restricted)).status_code == 200
    assert client.get("/api/users/sessions", headers=_bearer(other)).status_code == 200


def test_unrestricted_when_mandate_removed(client, test_user, test_admin_user, db_session):
    admin_token = _login(client, "admin", "adminpassword123").json()["token"]
    _force_2fa(client, admin_token, test_user.id, required=True)
    restricted = _login(client).json()["token"]

    _force_2fa(client, admin_token, test_user.id, required=False)

    assert client.get("/api/users/sessions", headers=_bearer(restricted)).status_code == 200
    assert _login(client).json()["requires_2fa_enrollment"] is False


def test_only_admins_can_set_the_mandate(client, test_user, test_admin_user, db_session):
    token = _login(client).json()["token"]
    resp = client.put(
        f"/api/admin/clients/{test_user.id}/require-2fa",
        json={"require_2fa": True},
        headers=_bearer(token),
    )
    assert resp.status_code in (401, 403)


# --- Admin password guards -------------------------------------------------


def test_admin_password_change_needs_actor_password(client, test_admin_user, db_session):
    actor = _login(client, "admin", "adminpassword123").json()["token"]
    other = _login(client, "admin", "adminpassword123").json()["token"]

    resp = client.put(
        f"/api/admin/admins/{test_admin_user.id}",
        json={"password": "brandnewadmin99"},
        headers=_bearer(actor),
    )
    assert resp.status_code == 403

    resp = client.put(
        f"/api/admin/admins/{test_admin_user.id}",
        json={"password": "brandnewadmin99", "current_password": "adminpassword123"},
        headers=_bearer(actor),
    )
    assert resp.status_code == 200
    # The target's sessions end with their password.
    assert client.get("/api/users/me", headers=_bearer(other)).status_code == 401


# --- Audit trail ---------------------------------------------------------


def test_successful_and_failed_logins_are_audited(client, test_user, db_session):
    _login(client, password="wrong-password")
    _login(client)
    actions = _audit_actions(db_session, test_user.id)
    assert "login_failed" in actions
    assert "login_success" in actions


def test_password_change_is_audited(client, test_user, db_session):
    token = _login(client).json()["token"]
    client.post(
        "/api/users/me/change-password",
        json={"current_password": "testpassword123", "new_password": "brandnewpassword1"},
        headers=_bearer(token),
    )
    assert "password_changed" in _audit_actions(db_session, test_user.id)


def test_2fa_enable_and_disable_are_audited(client, test_user, encryption_key, db_session):
    token = _login(client).json()["token"]
    setup = client.post(
        "/api/users/2fa/setup", json={"password": "testpassword123"}, headers=_bearer(token)
    ).json()
    client.post("/api/users/2fa/confirm", json={"code": pyotp.TOTP(setup["secret"]).now()}, headers=_bearer(token))
    assert "2fa_enabled" in _audit_actions(db_session, test_user.id)


def test_impersonation_is_audited(client, test_user, test_admin_user, db_session):
    admin_token = _login(client, "admin", "adminpassword123").json()["token"]
    resp = client.post(f"/api/admin/clients/{test_user.id}/impersonate", headers=_bearer(admin_token))
    assert resp.status_code == 200
    assert "impersonation_started" in _audit_actions(db_session, test_user.id)
