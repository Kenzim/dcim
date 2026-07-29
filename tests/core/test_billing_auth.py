from datetime import datetime

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.core.billing_auth import get_billing_integration, hash_api_key
from app.models.billing_integration import BillingIntegration


def _build_request(headers: dict[str, str] | None = None, client_ip: str = "127.0.0.1") -> Request:
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "client": (client_ip, 12345),
    }
    return Request(scope)


def test_get_billing_integration_requires_api_key(db_session):
    request = _build_request()

    with pytest.raises(HTTPException) as exc:
        get_billing_integration(request=request, credentials=None, db=db_session)

    assert exc.value.status_code == 401
    assert "API key required" in exc.value.detail


def test_get_billing_integration_rejects_unknown_key(db_session):
    request = _build_request()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="unknown-key")

    with pytest.raises(HTTPException) as exc:
        get_billing_integration(request=request, credentials=credentials, db=db_session)

    assert exc.value.status_code == 401
    assert "Invalid or disabled API key" in exc.value.detail


def test_get_billing_integration_updates_last_used_metadata(db_session):
    integration = BillingIntegration(
        name="WHMCS",
        integration_type="whmcs",
        api_key=hash_api_key("valid-key"),
        api_key_prefix="valid-ke",
        enabled=True,
    )
    db_session.add(integration)
    db_session.commit()
    db_session.refresh(integration)

    request = _build_request()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-key")

    result = get_billing_integration(request=request, credentials=credentials, db=db_session)

    assert result.id == integration.id
    assert result.last_used_ip == "127.0.0.1"
    assert isinstance(result.last_used_at, datetime)


def test_get_billing_integration_ignores_xff_by_default(db_session):
    """X-Forwarded-For must not be trusted unless settings.trust_x_forwarded_for
    is explicitly enabled (see app/core/billing_auth.py) -- otherwise any
    caller could spoof the IP recorded in last_used_ip for audit purposes."""
    integration = BillingIntegration(
        name="WHMCS",
        integration_type="whmcs",
        api_key=hash_api_key("valid-key-2"),
        api_key_prefix="valid-ke",
        enabled=True,
    )
    db_session.add(integration)
    db_session.commit()
    db_session.refresh(integration)

    request = _build_request(headers={"x-forwarded-for": "203.0.113.55"})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-key-2")

    result = get_billing_integration(request=request, credentials=credentials, db=db_session)

    assert result.last_used_ip == "127.0.0.1"


def test_get_billing_integration_trusts_xff_when_enabled(db_session, monkeypatch):
    from app.core.billing_auth import settings as billing_auth_settings

    monkeypatch.setattr(billing_auth_settings, "trust_x_forwarded_for", True)

    integration = BillingIntegration(
        name="WHMCS",
        integration_type="whmcs",
        api_key=hash_api_key("valid-key-3"),
        api_key_prefix="valid-ke",
        enabled=True,
    )
    db_session.add(integration)
    db_session.commit()
    db_session.refresh(integration)

    request = _build_request(headers={"x-forwarded-for": "203.0.113.55"})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-key-3")

    result = get_billing_integration(request=request, credentials=credentials, db=db_session)

    assert result.last_used_ip == "203.0.113.55"


def test_api_key_stored_hashed_and_authenticates(db_session):
    from app.dao.billing_integration_dao import BillingIntegrationDAO

    created = BillingIntegrationDAO.create(
        db_session, name="WHMCS Prod", integration_type="whmcs"
    )
    plaintext = created.plaintext_api_key
    assert plaintext  # returned once at creation

    # Stored value is the hash, not the plaintext.
    assert created.api_key == hash_api_key(plaintext)
    assert created.api_key != plaintext
    assert created.api_key_prefix == plaintext[:8]

    # Lookup and auth succeed with the plaintext key.
    assert BillingIntegrationDAO.get_by_api_key(db_session, plaintext).id == created.id

    request = _build_request()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=plaintext)
    result = get_billing_integration(request=request, credentials=credentials, db=db_session)
    assert result.id == created.id


def test_rotate_key_invalidates_old_key(db_session):
    from app.dao.billing_integration_dao import BillingIntegrationDAO

    created = BillingIntegrationDAO.create(
        db_session, name="Rotate", integration_type="whmcs"
    )
    old_key = created.plaintext_api_key

    rotated = BillingIntegrationDAO.rotate_api_key(db_session, created)
    new_key = rotated.plaintext_api_key
    assert new_key != old_key

    assert BillingIntegrationDAO.get_by_api_key(db_session, old_key) is None
    assert BillingIntegrationDAO.get_by_api_key(db_session, new_key).id == created.id
