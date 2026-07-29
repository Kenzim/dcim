"""
Tests for client_portal_service: non-admin users always get client portal
access (blank password by default) and billing SSO ticket handling.
"""
from app.models.billing_integration import BillingIntegration
from app.services.client_portal_service import (
    ensure_user_for_billing_identity,
    mint_sso_ticket,
    redeem_sso_ticket,
)


def _make_integration(db_session, name="whmcs-portal"):
    integration = BillingIntegration(
        name=name, integration_type="whmcs", api_key=f"k-{name}", enabled=True, config={}
    )
    db_session.add(integration)
    db_session.commit()
    db_session.refresh(integration)
    return integration


def test_ensure_user_creates_blank_password_by_default(db_session):
    """Non-admin users always get client portal access; default password is blank (disabled)."""
    integration = _make_integration(db_session)

    user = ensure_user_for_billing_identity(
        db_session,
        billing_integration_id=integration.id,
        external_user_id="ext-1",
        external_username="extuser",
        external_email="ext@example.com",
    )

    assert user.id is not None
    assert user.is_admin is False
    assert user.has_password is False
    assert user.password is None
    assert user.billing_integration_id == integration.id
    assert user.external_user_id == "ext-1"


def test_ensure_user_uses_password_when_provided(db_session):
    integration = _make_integration(db_session)

    user = ensure_user_for_billing_identity(
        db_session,
        billing_integration_id=integration.id,
        external_user_id="ext-2",
        password="realSecret123",
    )

    assert user.has_password is True
    assert user.verify_password("realSecret123") is True


def test_ensure_user_is_idempotent(db_session):
    """Calling twice for the same billing identity returns the same linked account."""
    integration = _make_integration(db_session)

    first = ensure_user_for_billing_identity(
        db_session, billing_integration_id=integration.id, external_user_id="ext-3"
    )
    second = ensure_user_for_billing_identity(
        db_session,
        billing_integration_id=integration.id,
        external_user_id="ext-3",
        password="shouldNotApply123",
    )

    assert first.id == second.id
    # Password from the second call must NOT overwrite an already-linked account.
    assert second.has_password is False


def test_ensure_user_refreshes_display_fields(db_session):
    integration = _make_integration(db_session)

    first = ensure_user_for_billing_identity(
        db_session,
        billing_integration_id=integration.id,
        external_user_id="ext-4",
        external_username="olduser",
        external_email="old@example.com",
    )
    assert first.external_username == "olduser"

    second = ensure_user_for_billing_identity(
        db_session,
        billing_integration_id=integration.id,
        external_user_id="ext-4",
        external_username="newuser",
        external_email="new@example.com",
    )

    assert second.id == first.id
    assert second.external_username == "newuser"
    assert second.external_email == "new@example.com"


def test_ensure_user_generates_unique_username_and_email(db_session):
    integration = _make_integration(db_session)

    user1 = ensure_user_for_billing_identity(
        db_session,
        billing_integration_id=integration.id,
        external_user_id="ext-5",
        external_username="dupuser",
        external_email="dup@example.com",
    )
    user2 = ensure_user_for_billing_identity(
        db_session,
        billing_integration_id=integration.id,
        external_user_id="ext-6",
        external_username="dupuser",
        external_email="dup@example.com",
    )

    assert user1.username != user2.username
    assert user1.email != user2.email


def test_ensure_user_scoped_per_integration(db_session):
    """Same external_user_id under different integrations are distinct users."""
    integration_a = _make_integration(db_session, name="whmcs-a")
    integration_b = _make_integration(db_session, name="whmcs-b")

    user_a = ensure_user_for_billing_identity(
        db_session, billing_integration_id=integration_a.id, external_user_id="ext-shared"
    )
    user_b = ensure_user_for_billing_identity(
        db_session, billing_integration_id=integration_b.id, external_user_id="ext-shared"
    )

    assert user_a.id != user_b.id


def test_sso_ticket_roundtrip(db_session, mock_redis, monkeypatch):
    import app.services.client_portal_service as client_portal_service_module

    monkeypatch.setattr(client_portal_service_module, "redis_client", mock_redis)

    token = mint_sso_ticket(user_id=42, ttl_seconds=60)
    assert token

    resolved_user_id = redeem_sso_ticket(token)
    assert resolved_user_id == 42

    # Single-use: redeeming again must fail.
    assert redeem_sso_ticket(token) is None


def test_redeem_sso_ticket_invalid_token_returns_none(mock_redis, monkeypatch):
    import app.services.client_portal_service as client_portal_service_module

    monkeypatch.setattr(client_portal_service_module, "redis_client", mock_redis)

    assert redeem_sso_ticket("not-a-real-token") is None
    assert redeem_sso_ticket("") is None
