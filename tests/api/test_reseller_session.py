from app.models.reseller import Reseller
from app.models.user import User


def test_reseller_role_round_trips_through_login_session_and_me(
    client, db_session
):
    user = User(
        username="session-reseller",
        email="session-reseller@example.com",
        is_reseller=True,
    )
    user.set_password("reseller-password")
    db_session.add(Reseller(user=user))
    db_session.commit()

    login = client.post(
        "/api/users/login",
        json={
            "username": "session-reseller",
            "password": "reseller-password",
        },
    )
    assert login.status_code == 200, login.text
    assert login.json()["is_reseller"] is True

    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    me = client.get("/api/users/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["is_reseller"] is True

    client_services = client.get(
        "/api/client/services/me", headers=headers
    )
    assert client_services.status_code == 403
