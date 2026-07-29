from app.models.reseller import Reseller
from app.models.user import User
from app.services.reseller_billing_service import ResellerBillingService


def _reseller(db_session, suffix):
    user = User(
        username=f"reseller-{suffix}",
        email=f"reseller-{suffix}@example.com",
        is_reseller=True,
    )
    reseller = Reseller(user=user)
    db_session.add(reseller)
    db_session.flush()
    return reseller


def test_client_identity_is_isolated_by_reseller(db_session):
    first = _reseller(db_session, "one")
    second = _reseller(db_session, "two")

    first_client = ResellerBillingService.ensure_client_user(
        db_session,
        reseller=first,
        external_user_id="shared-external-id",
        external_username="same-name",
        external_email="same@example.com",
    )
    second_client = ResellerBillingService.ensure_client_user(
        db_session,
        reseller=second,
        external_user_id="shared-external-id",
        external_username="same-name",
        external_email="same@example.com",
    )

    assert first_client.id != second_client.id
    assert first_client.reseller_id == first.id
    assert second_client.reseller_id == second.id
    assert first_client.username != second_client.username
    assert first_client.email != second_client.email
    assert first_client.billing_integration_id is None
    assert second_client.billing_integration_id is None
