from datetime import datetime

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.core.billing_auth import get_billing_integration
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
        api_key="valid-key",
        enabled=True,
    )
    db_session.add(integration)
    db_session.commit()
    db_session.refresh(integration)

    request = _build_request(headers={"x-forwarded-for": "203.0.113.55"})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-key")

    result = get_billing_integration(request=request, credentials=credentials, db=db_session)

    assert result.id == integration.id
    assert result.last_used_ip == "203.0.113.55"
    assert isinstance(result.last_used_at, datetime)
