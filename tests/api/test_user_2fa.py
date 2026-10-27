"""Account 2FA: enrollment, two-step login, replay protection and recovery codes."""

import time

import pyotp
import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.models.commerce_auth_extra import UserTotpSecret


@pytest.fixture
def encryption_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "service_instance_encryption_key", key)
    return key


def _login(client, password="testpassword123"):
    return client.post("/api/users/login", json={"username": "testuser", "password": password})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _enroll(client, token, password="testpassword123"):
    setup = client.post("/api/users/2fa/setup", json={"password": password}, headers=_bearer(token))
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    code = pyotp.TOTP(secret).now()
    confirm = client.post("/api/users/2fa/confirm", json={"code": code}, headers=_bearer(token))
    assert confirm.status_code == 200, confirm.text
    return secret, confirm.json()["recovery_codes"]


def _code_for_later_step(secret, db_session, user_id):
    """A TOTP code for a step strictly after the last accepted one.

    Using the next step keeps the test independent of where in the 30-second
    window it runs, and of which step enrollment already consumed.
    """
    row = db_session.get(UserTotpSecret, user_id)
    totp = pyotp.TOTP(secret)
    current = int(time.time() // totp.interval)
    step = current + 1
    if row.last_used_step is not None and step <= row.last_used_step:
        step = row.last_used_step + 1
    return totp.at(step * totp.interval)


def test_login_without_2fa_still_returns_session(client, test_user):
    resp = _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["requires_2fa"] is False


def test_setup_requires_current_password(client, test_user, encryption_key):
    token = _login(client).json()["token"]
    resp = client.post("/api/users/2fa/setup", json={"password": "wrong"}, headers=_bearer(token))
    assert resp.status_code == 401


def test_setup_refuses_without_encryption_key(client, test_user, monkeypatch):
    monkeypatch.setattr(settings, "service_instance_encryption_key", None)
    token = _login(client).json()["token"]
    resp = client.post("/api/users/2fa/setup", json={"password": "testpassword123"}, headers=_bearer(token))
    assert resp.status_code == 503


def test_enrolment_returns_ten_recovery_codes_once(client, test_user, encryption_key):
    token = _login(client).json()["token"]
    _, codes = _enroll(client, token)
    assert len(codes) == 10
    assert len(set(codes)) == 10

    status = client.get("/api/users/2fa/status", headers=_bearer(token)).json()
    assert status == {"enabled": True, "recovery_codes_remaining": 10, "enrollment_required": False}


def test_login_with_2fa_returns_challenge_not_session(client, test_user, encryption_key):
    token = _login(client).json()["token"]
    _enroll(client, token)

    resp = _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["requires_2fa"] is True
    assert body["pending_token"]
    assert body["token"] is None


def test_two_step_login_with_totp_succeeds(client, test_user, encryption_key, db_session):
    token = _login(client).json()["token"]
    secret, _ = _enroll(client, token)

    pending = _login(client).json()["pending_token"]
    code = _code_for_later_step(secret, db_session, test_user.id)
    resp = client.post("/api/users/login/2fa", json={"pending_token": pending, "code": code})
    assert resp.status_code == 200, resp.text
    session_token = resp.json()["token"]
    assert session_token
    assert client.get("/api/users/me", headers=_bearer(session_token)).status_code == 200


def test_wrong_totp_code_is_rejected(client, test_user, encryption_key):
    token = _login(client).json()["token"]
    _enroll(client, token)

    pending = _login(client).json()["pending_token"]
    resp = client.post("/api/users/login/2fa", json={"pending_token": pending, "code": "000000"})
    assert resp.status_code == 401


def test_pending_token_cannot_be_reused_after_success(client, test_user, encryption_key, db_session):
    token = _login(client).json()["token"]
    secret, _ = _enroll(client, token)

    pending = _login(client).json()["pending_token"]
    code = _code_for_later_step(secret, db_session, test_user.id)
    assert client.post("/api/users/login/2fa", json={"pending_token": pending, "code": code}).status_code == 200

    resp = client.post("/api/users/login/2fa", json={"pending_token": pending, "code": code})
    assert resp.status_code == 401


def test_totp_code_cannot_be_replayed(client, test_user, encryption_key, db_session):
    token = _login(client).json()["token"]
    secret, _ = _enroll(client, token)
    code = _code_for_later_step(secret, db_session, test_user.id)

    first = client.post(
        "/api/users/login/2fa",
        json={"pending_token": _login(client).json()["pending_token"], "code": code},
    )
    assert first.status_code == 200

    # Same code, fresh challenge: the step is already consumed.
    second = client.post(
        "/api/users/login/2fa",
        json={"pending_token": _login(client).json()["pending_token"], "code": code},
    )
    assert second.status_code == 401


def test_recovery_code_works_exactly_once(client, test_user, encryption_key):
    token = _login(client).json()["token"]
    _, codes = _enroll(client, token)
    recovery = codes[0]

    first = client.post(
        "/api/users/login/2fa",
        json={"pending_token": _login(client).json()["pending_token"], "code": recovery},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/users/login/2fa",
        json={"pending_token": _login(client).json()["pending_token"], "code": recovery},
    )
    assert second.status_code == 401

    status = client.get("/api/users/2fa/status", headers=_bearer(first.json()["token"])).json()
    assert status["recovery_codes_remaining"] == 9


def test_recovery_code_accepted_without_spacing_or_case(client, test_user, encryption_key):
    token = _login(client).json()["token"]
    _, codes = _enroll(client, token)
    mangled = codes[1].replace("-", "").upper()

    resp = client.post(
        "/api/users/login/2fa",
        json={"pending_token": _login(client).json()["pending_token"], "code": mangled},
    )
    assert resp.status_code == 200


def test_regenerate_invalidates_old_recovery_codes(client, test_user, encryption_key):
    token = _login(client).json()["token"]
    _, old_codes = _enroll(client, token)

    regen = client.post(
        "/api/users/2fa/recovery-codes",
        json={"code": old_codes[0]},
        headers=_bearer(token),
    )
    assert regen.status_code == 200
    new_codes = regen.json()["recovery_codes"]
    assert len(new_codes) == 10
    assert set(new_codes).isdisjoint(old_codes)

    stale = client.post(
        "/api/users/login/2fa",
        json={"pending_token": _login(client).json()["pending_token"], "code": old_codes[2]},
    )
    assert stale.status_code == 401


def test_disable_requires_valid_code_then_removes_2fa(client, test_user, encryption_key, db_session):
    token = _login(client).json()["token"]
    secret, codes = _enroll(client, token)

    bad = client.post("/api/users/2fa/disable", json={"code": "000000"}, headers=_bearer(token))
    assert bad.status_code == 400

    code = _code_for_later_step(secret, db_session, test_user.id)
    ok = client.post("/api/users/2fa/disable", json={"code": code}, headers=_bearer(token))
    assert ok.status_code == 200

    assert _login(client).json()["requires_2fa"] is False


def test_reenrolment_blocked_while_enabled(client, test_user, encryption_key):
    token = _login(client).json()["token"]
    _enroll(client, token)
    resp = client.post("/api/users/2fa/setup", json={"password": "testpassword123"}, headers=_bearer(token))
    assert resp.status_code == 409


def test_password_change_revokes_other_sessions(client, test_user):
    first = _login(client).json()["token"]
    second = _login(client).json()["token"]

    resp = client.post(
        "/api/users/me/change-password",
        json={"current_password": "testpassword123", "new_password": "brandnewpassword1"},
        headers=_bearer(second),
    )
    assert resp.status_code == 200
    assert client.get("/api/users/me", headers=_bearer(first)).status_code == 401
    assert client.get("/api/users/me", headers=_bearer(second)).status_code == 200


def test_legacy_plaintext_secret_still_accepted_at_login(client, test_user, db_session, monkeypatch):
    """Rows enrolled before encryption was configured must not lock the user out."""
    monkeypatch.setattr(settings, "service_instance_encryption_key", None)
    secret = pyotp.random_base32()
    db_session.add(
        UserTotpSecret(
            user_id=test_user.id,
            secret_encrypted=secret,
            enabled=True,
            recovery_codes_hash=[],
        )
    )
    db_session.commit()

    pending = _login(client).json()["pending_token"]
    code = _code_for_later_step(secret, db_session, test_user.id)
    resp = client.post("/api/users/login/2fa", json={"pending_token": pending, "code": code})
    assert resp.status_code == 200, resp.text
