"""Public commerce registration and password-reset endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.dao.user_dao import UserDAO
from app.models.commerce_auth_extra import EmailVerificationToken, PasswordResetToken
from app.api.commerce_auth_public import EmailMessageService, _hash_token

EMAIL = "newshop@example.com"


def _enable_open_registration(monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    monkeypatch.setattr(settings, "commerce_registration_mode", "open")
    monkeypatch.setattr(settings, "commerce_public_app_url", "https://shop.example")
    monkeypatch.setattr(settings, "commerce_default_currency", "USD")


def test_register_verify_and_password_reset(client, db_session, monkeypatch):
    _enable_open_registration(monkeypatch)
    monkeypatch.setattr(EmailMessageService, "enqueue", staticmethod(lambda *a, **k: None))
    payload = {"username": "newshop", "email": EMAIL, "password": "password123"}
    monkeypatch.setattr(settings, "commerce_registration_mode", "disabled")
    denied = client.post("/api/commerce/auth/register", json=payload)
    assert denied.status_code == 403

    monkeypatch.setattr(settings, "commerce_registration_mode", "invite_only")
    invited = client.post("/api/commerce/auth/register", json=payload)
    assert invited.status_code == 403

    created = client.post(
        "/api/commerce/auth/register",
        json={**payload, "invite_token": "invite-token-value"},
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["user_id"]
    monkeypatch.setattr(settings, "commerce_registration_mode", "open")
    dup_user = client.post(
        "/api/commerce/auth/register",
        json={"username": "newshop", "email": "other@example.com", "password": "password123"},
    )
    assert dup_user.status_code == 409
    dup_email = client.post(
        "/api/commerce/auth/register",
        json={"username": "newshop2", "email": EMAIL, "password": "password123"},
    )
    assert dup_email.status_code == 409

    raw = "verify-token-value-123456"
    db_session.add(
        EmailVerificationToken(
            user_id=user_id,
            token_hash=_hash_token(raw),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    db_session.commit()
    assert client.post("/api/commerce/auth/verify-email", json={"token": "no-such-token-value"}).status_code == 400
    verified = client.post("/api/commerce/auth/verify-email", json={"token": raw})
    assert verified.status_code == 200
    assert verified.json()["verified"] is True

    expired_raw = "expired-token-value-1234"
    db_session.add(
        EmailVerificationToken(
            user_id=user_id,
            token_hash=_hash_token(expired_raw),
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    db_session.commit()
    assert client.post("/api/commerce/auth/verify-email", json={"token": expired_raw}).status_code == 400

    monkeypatch.setattr(settings, "trust_x_forwarded_for", True)
    reset = client.post(
        "/api/commerce/auth/password-reset/request",
        json={"email": EMAIL},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert reset.status_code == 200
    assert reset.json()["ok"] is True

    reset_raw = "reset-token-value-123456"
    db_session.add(
        PasswordResetToken(
            user_id=user_id,
            token_hash=_hash_token(reset_raw),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    db_session.commit()
    assert client.post(
        "/api/commerce/auth/password-reset/confirm",
        json={"token": "missing-reset-token-xx", "password": "newpass123"},
    ).status_code == 400
    confirm = client.post(
        "/api/commerce/auth/password-reset/confirm",
        json={"token": reset_raw, "password": "newpass123"},
    )
    assert confirm.status_code == 200, confirm.text
    user = UserDAO.get_by_id(db_session, user_id)
    assert user.verify_password("newpass123")

    expired_reset = "expired-reset-token-xx"
    db_session.add(
        PasswordResetToken(
            user_id=user_id,
            token_hash=_hash_token(expired_reset),
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    db_session.commit()
    assert client.post(
        "/api/commerce/auth/password-reset/confirm",
        json={"token": expired_reset, "password": "newerpass1"},
    ).status_code == 400

    monkeypatch.setattr(
        EmailMessageService,
        "enqueue",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(ValueError("no template"))),
    )
    again = client.post("/api/commerce/auth/password-reset/request", json={"email": EMAIL})
    assert again.status_code == 200
