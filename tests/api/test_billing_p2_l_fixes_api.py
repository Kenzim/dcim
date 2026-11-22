"""Cancellation routes after the P2-L review: reseller key scope, client double submit."""

from __future__ import annotations

from app.models.billing_jobs import CancellationRequest, CancellationStatus
from app.models.reseller import Reseller
from app.services.service_lifecycle import ServiceLifecycle, ServiceLifecycleError
from tests.api.test_billing_p2_cancellations_api import _bearer, _service, _setup, _user  # noqa: F401


def test_a_reseller_reusing_another_resellers_key_makes_a_new_request(client, db_session):
    first = Reseller(user=_user(db_session, "p2l-key-a", is_reseller=True))
    second = Reseller(user=_user(db_session, "p2l-key-b", is_reseller=True))
    db_session.add_all([first, second])
    db_session.commit()
    first_service, _ = _service(db_session, reseller=first)
    second_service, _ = _service(db_session, reseller=second)

    made = []
    for username, service in (("p2l-key-a", first_service), ("p2l-key-b", second_service)):
        response = client.post(
            "/api/reseller-panel/cancellation-requests",
            headers={**_bearer(client, username), "Idempotency-Key": "shared-key"},
            json={"service_id": service.id},
        )
        assert response.status_code == 200, response.text
        made.append(response.json()["request_id"])
    assert made[0] != made[1]
    keys = [row.idempotency_key for row in db_session.query(CancellationRequest).order_by(CancellationRequest.id)]
    assert keys[0] != keys[1] and all(len(key) <= 128 for key in keys)
    # The same reseller's replay is still the same request.
    replay = client.post(
        "/api/reseller-panel/cancellation-requests",
        headers={**_bearer(client, "p2l-key-a"), "Idempotency-Key": "shared-key"},
        json={"service_id": first_service.id},
    )
    assert replay.json()["request_id"] == made[0]


def test_a_double_submit_of_an_immediate_cancel_powers_off_once(client, db_session, monkeypatch):
    attempts = []

    async def failing_power_off(self, db, service):
        attempts.append(service.id)
        raise ServiceLifecycleError("Failed to power off before suspend")

    monkeypatch.setattr(ServiceLifecycle, "ensure_powered_off", failing_power_off)
    owner = _user(db_session, "p2l-double")
    service, _ = _service(db_session, owner=owner)
    headers = _bearer(client, "p2l-double")

    responses = [
        client.post(f"/api/client/commerce/services/{service.id}/cancel", headers=headers, json={"when": "immediate"})
        for _ in range(2)
    ]

    assert [response.status_code for response in responses] == [200, 200], [r.text for r in responses]
    assert responses[0].json()["termination_pending"] is True
    assert responses[0].json()["request_id"] == responses[1].json()["request_id"]
    assert attempts == [service.id]
    rows = db_session.query(CancellationRequest).all()
    assert len(rows) == 1 and rows[0].status == CancellationStatus.SCHEDULED
