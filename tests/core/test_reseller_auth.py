from starlette.requests import Request
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import pytest

from app.core.reseller_auth import (
    get_reseller_by_api_key,
    hash_reseller_api_key,
    issue_reseller_api_key,
)
from app.core.auth import require_reseller
from app.models.reseller import Reseller, ResellerStatus
from app.models.user import User


def _request(client_ip="127.0.0.1", forwarded_for=None):
    headers = []
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (client_ip, 1234),
        }
    )


def _reseller(db_session):
    user = User(
        username="auth-reseller",
        email="auth-reseller@example.com",
        is_reseller=True,
    )
    reseller = Reseller(user=user)
    db_session.add(reseller)
    db_session.flush()
    plaintext = issue_reseller_api_key(db_session, reseller)
    db_session.commit()
    return reseller, plaintext


def test_reseller_key_is_hashed_and_dedicated_auth_updates_usage(db_session):
    reseller, plaintext = _reseller(db_session)
    assert reseller.api_key_hash == hash_reseller_api_key(plaintext)
    assert reseller.api_key_hash != plaintext
    assert reseller.api_key_prefix == plaintext[:16]

    result = get_reseller_by_api_key(
        request=_request(forwarded_for="203.0.113.50"),
        credentials=HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=plaintext
        ),
        db=db_session,
    )
    assert result.id == reseller.id
    # XFF is ignored unless TRUST_X_FORWARDED_FOR is explicitly enabled.
    assert result.last_used_ip == "127.0.0.1"
    assert result.last_used_at is not None


@pytest.mark.parametrize(
    "reseller_status",
    [ResellerStatus.SUSPENDED, ResellerStatus.DISABLED],
)
def test_reseller_auth_rejects_inactive_keys(db_session, reseller_status):
    reseller, plaintext = _reseller(db_session)
    reseller.status = reseller_status
    db_session.commit()
    request = _request()
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=plaintext
    )

    with pytest.raises(HTTPException) as exc:
        get_reseller_by_api_key(
            request=request,
            credentials=credentials,
            db=db_session,
        )
    assert exc.value.status_code == 401


def test_require_reseller_accepts_only_reseller_user_sessions():
    accepted = require_reseller(
        {"user_id": 42, "is_reseller": True, "is_admin": False}
    )
    assert accepted["user_id"] == 42

    integration_auth = {
        "type": "api_key",
        "user_id": 42,
        "is_reseller": True,
    }
    with pytest.raises(HTTPException) as exc:
        require_reseller(integration_auth)
    assert exc.value.status_code == 403
