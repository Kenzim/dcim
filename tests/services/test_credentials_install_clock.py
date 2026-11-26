"""The install time stored with a server's keys is a UTC instant, whatever the database clock says."""
from datetime import datetime, timedelta, timezone

from app.dao.billing_integration_dao import BillingIntegrationDAO
from tests.api.test_billing_credentials_isolation import (  # noqa: F401  (_install_mocks is autouse)
    UBUNTU,
    UBUNTU_PARAMS,
    _admin_install,
    _billing_install,
    _install_mocks,
    _server,
    _service,
    _stored,
    _user,
)


def _assert_utc_now(value):
    installed = datetime.fromisoformat(value)
    assert installed.utcoffset() == timedelta(0), value
    assert abs(datetime.now(timezone.utc) - installed) < timedelta(minutes=5)


def test_staff_install_stamps_the_install_time_in_utc(db_session):
    server = _server(db_session, "clk-a", "198.51.100.180")
    _admin_install(db_session, server, UBUNTU.id, dict(UBUNTU_PARAMS))
    _assert_utc_now(_stored(db_session, server)["last_updated"])


def test_billing_install_stamps_the_install_time_in_utc(db_session):
    integration = BillingIntegrationDAO.create(db_session, name="clk-b", integration_type="whmcs")
    server = _server(db_session, "clk-b-srv", "198.51.100.181")
    service = _service(db_session, server, _user(db_session, integration, "clk-b-u"), "clk-b-svc")
    _billing_install(db_session, service, UBUNTU.id, dict(UBUNTU_PARAMS))
    _assert_utc_now(_stored(db_session, server)["last_updated"])
