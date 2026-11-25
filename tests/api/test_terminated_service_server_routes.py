"""A terminated or superseded service cannot act on the server's current occupant.

Every route that resolves a service's server refuses (409) when the service is
terminated or another live service holds the same server. Nothing here reaches
a real machine: plugin lookup is replaced by a recorder that must stay unused.
"""
import pytest

import app.api.billing as billing
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.models.reseller import ServiceBilling
from app.models.service import ServiceStatus
from app.services.service_resource import service_linked_server
from tests.api.test_billing_credentials_isolation import _server, _service, _user
from tests.api.test_reseller_api import _headers as reseller_headers
from tests.api.test_reseller_api import _reseller

BILLING_ROUTES = [
    ("get", "status", None),
    ("get", "usage", None),
    ("get", "debug", None),
    ("post", "ipmi-ticket", None),
    ("post", "kvm-ticket", None),
    ("post", "sol-ticket", None),
    ("post", "power", {"action": "off"}),
    ("post", "actions/run-script", {"script_id": 1}),
    ("post", "actions/reinstall-os", {"template_id": "ubuntu-cloud-image"}),
    ("post", "sol/send", {"data": "x"}),
    ("get", "virtual-media", None),
    ("post", "virtual-media/insert", {"filename": "a.iso"}),
    ("post", "virtual-media/eject", None),
    ("post", "windows-password-reset", {}),
]
RESELLER_ROUTES = [("get", "status", None), ("post", "power", {"action": "off"})]
SCENARIOS = ["terminated_and_resold", "terminated_alone", "superseded"]


@pytest.fixture(autouse=True)
def _no_machine_access(monkeypatch):
    calls = []

    async def plugin(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a plugin was reached")

    monkeypatch.setattr(billing, "_billing_get_plugin_instance", plugin)
    monkeypatch.setattr(
        billing, "get_registry", lambda: (_ for _ in ()).throw(AssertionError("registry reached"))
    )
    return calls


def _world(db, name, scenario):
    integration = BillingIntegrationDAO.create(db, name=name, integration_type="whmcs")
    server = _server(db, f"{name}-srv", f"198.51.100.{100 + len(name)}")
    first = _service(db, server, _user(db, integration, f"{name}-1"), f"{name}-svc-1")
    if scenario == "terminated_and_resold":
        first.status = ServiceStatus.TERMINATED
        db.commit()
    if scenario == "terminated_alone":
        first.status = ServiceStatus.TERMINATED
        db.commit()
    else:
        _service(db, server, _user(db, integration, f"{name}-2"), f"{name}-svc-2")
    return integration, server, first


def _bheaders(integration):
    return {"Authorization": f"Bearer {integration.plaintext_api_key}"}


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("method,path,body", BILLING_ROUTES, ids=[r[1] for r in BILLING_ROUTES])
def test_billing_route_refuses_service_without_server(client, db_session, method, path, body, scenario):
    integration, _, service = _world(db_session, f"b-{path.replace('/', '-')}-{scenario}"[:40], scenario)
    kwargs = {"json": body} if body is not None else {}
    resp = getattr(client, method)(
        f"/api/billing/services/{service.id}/{path}", headers=_bheaders(integration), **kwargs
    )
    assert resp.status_code == 409, (resp.status_code, resp.text)
    assert "cannot act on that" in resp.text


def _reseller_service(db, service, reseller):
    db.add(ServiceBilling(service_id=service.id, reseller_id=reseller.id, monthly_price_cents=100))
    db.commit()


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("method,path,body", RESELLER_ROUTES, ids=[r[1] for r in RESELLER_ROUTES])
def test_reseller_route_refuses_service_without_server(client, db_session, method, path, body, scenario):
    _, _, service = _world(db_session, f"r-{path}-{scenario}"[:40], scenario)
    reseller, key = _reseller(db_session, f"rt-{path}-{scenario}"[:30])
    _reseller_service(db_session, service, reseller)
    kwargs = {"json": body} if body is not None else {}
    resp = getattr(client, method)(
        f"/api/reseller/services/{service.id}/{path}", headers=reseller_headers(key), **kwargs
    )
    assert resp.status_code == 409, (resp.status_code, resp.text)
    assert "cannot act on that" in resp.text


def test_current_live_service_still_resolves_its_server(db_session):
    integration, server, service = _world(db_session, "live-one", "terminated_alone")
    service.status = ServiceStatus.ACTIVE
    db_session.commit()
    assert service_linked_server(db_session, service).id == server.id


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_history_opt_in_still_reaches_the_stored_link(db_session, scenario):
    _, server, service = _world(db_session, f"hist-{scenario}"[:30], scenario)
    assert service_linked_server(db_session, service) is None
    assert service_linked_server(db_session, service, include_history=True).id == server.id


def _locked_live_service(db, name):
    integration = BillingIntegrationDAO.create(db, name=name, integration_type="whmcs")
    server = _server(db, f"{name}-srv", f"198.51.100.{150 + len(name)}")
    server.locked = True
    server.lock_reason = "customer data"
    db.commit()
    service = _service(db, server, _user(db, integration, f"{name}-u"), f"{name}-svc")
    return integration, service


@pytest.mark.parametrize("action", ["on", "off", "reboot", "reset"])
def test_billing_power_refused_on_locked_server_for_every_action(
    client, db_session, _no_machine_access, action
):
    integration, service = _locked_live_service(db_session, f"lock-b-{action}")
    resp = client.post(
        f"/api/billing/services/{service.id}/power",
        headers=_bheaders(integration),
        json={"action": action},
    )
    assert resp.status_code == 409, resp.text
    assert "locked" in resp.text.lower()
    assert _no_machine_access == []


@pytest.mark.parametrize("action", ["on", "off", "reboot", "reset"])
def test_reseller_power_refused_on_locked_server_for_every_action(
    client, db_session, _no_machine_access, action
):
    _, service = _locked_live_service(db_session, f"lock-r-{action}")
    reseller, key = _reseller(db_session, f"lock-r-{action}")
    _reseller_service(db_session, service, reseller)
    resp = client.post(
        f"/api/reseller/services/{service.id}/power",
        headers=reseller_headers(key),
        json={"action": action},
    )
    assert resp.status_code == 409, resp.text
    assert "locked" in resp.text.lower()
    assert _no_machine_access == []
