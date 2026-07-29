from app.models.reseller import (
    Reseller,
    ResellerChargePreference,
    ResellerGroup,
    ResellerStatus,
)
from app.models.user import User


def test_reseller_defaults_and_unambiguous_user_relationships(db_session):
    account_user = User(
        username="reseller-account",
        email="reseller-account@example.com",
        is_reseller=True,
    )
    group = ResellerGroup(name="Gold Partners", code="gold")
    reseller = Reseller(user=account_user, group=group)
    client_user = User(
        username="reseller-client",
        email="reseller-client@example.com",
        owning_reseller=reseller,
    )
    db_session.add_all([reseller, client_user])
    db_session.flush()

    assert reseller.cached_balance_cents == 0
    assert reseller.charge_preference == ResellerChargePreference.CREDIT_FIRST
    assert reseller.status == ResellerStatus.ACTIVE
    assert group.enabled is True

    assert account_user.reseller_account is reseller
    assert reseller.user is account_user
    assert account_user.owning_reseller is None
    assert client_user.reseller_account is None
    assert client_user.owning_reseller is reseller
    assert client_user in reseller.clients
