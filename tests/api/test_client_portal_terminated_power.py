"""The client portal power route refuses a terminated or superseded service with 409."""
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.models.service import ServiceStatus
from tests.api.test_client_service_detail_power import (
    _bare_metal_service,
    _client_user,
    _login,
    _server,
)


@pytest.mark.parametrize("scenario", ["terminated_alone", "terminated_and_resold", "superseded"])
@pytest.mark.parametrize("action", ["on", "off", "reboot"])
def test_client_power_refuses_service_without_server(client, db_session, scenario, action):
    tag = f"{scenario}-{action}"
    user = _client_user(db_session, f"cpt-{tag}"[:30])
    other = _client_user(db_session, f"cpt-o-{tag}"[:30])
    server = _server(db_session, f"cpt-srv-{tag}"[:30], f"10.41.0.{len(scenario) + len(action)}")
    first = _bare_metal_service(db_session, owner_user_id=user.id, name=f"cpt-{tag}-1", server=server)
    if scenario != "superseded":
        first.status = ServiceStatus.TERMINATED
        db_session.commit()
    if scenario != "terminated_alone":
        _bare_metal_service(db_session, owner_user_id=other.id, name=f"cpt-{tag}-2", server=server)
    token = _login(client, user.username)
    with patch("app.api.services_client.get_registry") as registry:
        plugin = Mock()
        registry.return_value.get_plugin.return_value = plugin
        resp = client.post(
            f"/api/services/{first.id}/power",
            json={"action": action},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409, (resp.status_code, resp.text)
        registry.return_value.get_plugin.assert_not_called()
