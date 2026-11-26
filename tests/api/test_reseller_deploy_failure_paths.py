"""A failed reseller deploy always compensates and always attempts the clean-up (fabc36cb).

The deploy commits the service before it commits the billing record. When
anything fails in between, the compensation (refund and void) and the clean-up
(delete what this request inserted) must not depend on each other, and a
service must never be refunded after its billing was committed.

The tests assert outcomes (rows, balance, invoice status, logs) rather than the
internals of the compensation, so they hold on the production release as well
as on the billing branch.
"""

import logging
import types
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api import reseller as reseller_api
from app.models.reseller import (
    CreditLedgerEntry,
    Invoice,
    InvoiceStatus,
    ResellerProvisioningRequest,
    ServiceBilling,
)
from app.models.server_activity import ServerActivity
from app.models.service import Service, ServiceStatus, ServiceType
from app.services.provisioning.errors import ProvisioningError
from app.services.reseller_billing_service import ResellerBillingService
from tests.api.test_reseller_api import (
    _allow,
    _create_body,
    _headers,
    _product,
    _reseller,
    _vm_product,
)
from tests.api.test_reseller_deploy_cleanup import _post_retry, _retry_setup

FAR_FUTURE = datetime(2999, 1, 1)
START = 10_000
CHARGE = 3000


def _setup(client, db_session, kind, suffix):
    reseller, key = _reseller(db_session, f"fp-{suffix}", credit_cents=START)
    if kind == "vm":
        product = _vm_product(db_session)
        url = "/api/reseller/vm/services"
        body = {
            "name": f"doomed-{suffix}",
            "external_service_id": f"doomed-ext-{suffix}",
            "external_user_id": "fp-client",
            "product_code": product.code,
            "auto_provision": False,
        }
    else:
        product = _product(db_session)
        url = "/api/reseller/bare-metal/services"
        body = _create_body(name=f"doomed-{suffix}", external_service_id=f"doomed-ext-{suffix}")
    _allow(db_session, reseller, product)
    return reseller, key, url, body


def _deploy(client, key, url, body, idempotency="fp-deploy"):
    return client.post(url, headers=_headers(key, idempotency), json=body)


def _services(db_session, name):
    db_session.expire_all()
    return db_session.query(Service).filter(Service.name == name).all()


def _drop_activity(db, service):
    """The SQLite test schema has a service-or-server check on activity rows that
    the migrated MariaDB schema lacks; drop them so a real delete can run."""
    db.query(ServerActivity).filter(ServerActivity.service_id == service.id).delete()
    db.commit()


def _finalize_raises(monkeypatch, exc):
    def fail(db, *, service, **_kwargs):
        _drop_activity(db, service)
        raise exc

    monkeypatch.setattr("app.api.reseller.ResellerBillingService.finalize_deploy", fail)


def _compensation_raises(monkeypatch, calls=None):
    def fail(*_args, **_kwargs):
        if calls is not None:
            calls.append(1)
        raise OperationalError("UPDATE resellers", {}, Exception("Lock wait timeout exceeded"))

    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.compensate_failed_deploy", fail
    )


def _balance(db_session, reseller):
    db_session.expire_all()
    db_session.refresh(reseller)
    return reseller.cached_balance_cents


def _void_invoices(db_session):
    db_session.expire_all()
    return [i for i in db_session.query(Invoice).all() if i.status == InvoiceStatus.VOID]


# ---------------------------------------------------------------- failed compensation


@pytest.mark.parametrize("kind", ["proxy", "vm"])
def test_failed_compensation_still_deletes_the_service(
    client, db_session, monkeypatch, kind
):
    """Ruling 87c811ce: the clean-up still removes the service when the refund fails."""
    reseller, key, url, body = _setup(client, db_session, kind, f"comp-{kind}")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))
    _compensation_raises(monkeypatch)

    with pytest.raises(Exception):
        _deploy(client, key, url, body)

    assert _services(db_session, body["name"]) == []


@pytest.mark.parametrize("kind", ["proxy", "vm"])
def test_failed_compensation_returns_the_original_error(
    client, db_session, monkeypatch, kind
):
    reseller, key, url, body = _setup(client, db_session, kind, f"orig-{kind}")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))
    _compensation_raises(monkeypatch)

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _deploy(client, key, url, body)


def test_failed_compensation_after_a_provisioning_error_still_answers_with_that_error(
    client, db_session, monkeypatch
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "prov")
    _finalize_raises(monkeypatch, ProvisioningError("boom", "provisioning refused"))
    _compensation_raises(monkeypatch)

    response = _deploy(client, key, url, body)

    assert 400 <= response.status_code < 500, response.text
    assert "provisioning refused" in response.text
    assert _services(db_session, body["name"]) == []


def test_failed_compensation_logs_reseller_invoice_and_amount_for_a_manual_refund(
    client, db_session, monkeypatch, caplog
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "refundlog")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))
    _compensation_raises(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="app.api.reseller"):
        with pytest.raises(Exception):
            _deploy(client, key, url, body)

    invoice = db_session.query(Invoice).order_by(Invoice.id.desc()).first()
    assert invoice.amount_cents == CHARGE
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "charge not returned: refund by hand" in m
        and f"reseller {reseller.id}" in m
        and f"invoice {invoice.id}" in m
        and str(CHARGE) in m
        for m in errors
    ), errors


def test_failed_compensation_on_a_resumed_order_keeps_the_service_and_the_original_error(
    client, db_session, monkeypatch
):
    key, body, retried_id = _retry_setup(client, db_session, "fp-resume")

    def resume_fails(db, service, req, actor):
        raise RuntimeError("resume exploded")

    monkeypatch.setattr(
        "app.services.provisioning.service.resume_bare_metal_order", resume_fails
    )
    _compensation_raises(monkeypatch)

    with pytest.raises(RuntimeError, match="resume exploded"):
        _post_retry(client, key, body, "fp-resume-deploy")

    db_session.expire_all()
    assert db_session.get(Service, retried_id) is not None


# ---------------------------------------------------------------- a poisoned session


def test_a_failed_flush_in_finalize_is_rolled_back_so_the_deploy_is_compensated(
    client, db_session, monkeypatch
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "poison")

    def finalize_hits_a_constraint(db, *, service, **_kwargs):
        _drop_activity(db, service)
        for _ in range(2):
            db.add(ServiceBilling(service_id=service.id, monthly_price_cents=2000))
        db.flush()

    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.finalize_deploy",
        finalize_hits_a_constraint,
    )

    with pytest.raises(IntegrityError):
        _deploy(client, key, url, body)

    assert _services(db_session, body["name"]) == []
    assert _balance(db_session, reseller) == START
    assert len(_void_invoices(db_session)) == 1
    assert db_session.query(ServiceBilling).count() == 0


# ---------------------------------------------------------------- the clean-up


def test_a_failing_cancel_of_queued_installs_does_not_skip_the_service_delete(
    client, db_session, monkeypatch
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "cancel")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))

    def cancel_fails(db, trail):
        raise RuntimeError("cancel exploded")

    monkeypatch.setattr(reseller_api, "_cancel_queued_installs", cancel_fails)

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _deploy(client, key, url, body)

    assert _services(db_session, body["name"]) == []


def test_a_clean_up_that_cannot_delete_is_logged_and_the_original_error_returned(
    client, db_session, monkeypatch, caplog
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "delfail")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))

    def delete_fails(db, service_id):
        raise RuntimeError("delete exploded")

    monkeypatch.setattr("app.api.reseller.ServiceDAO.delete", delete_fails)

    with caplog.at_level(logging.ERROR, logger="app.api.reseller"):
        with pytest.raises(RuntimeError, match="finalize exploded"):
            _deploy(client, key, url, body)

    (leftover,) = _services(db_session, body["name"])
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        f"reseller {reseller.id}" in m
        and f"owner {leftover.owner_user_id}" in m
        and body["external_service_id"] in m
        and f"service {leftover.id}" in m
        for m in errors
    ), errors
    assert _balance(db_session, reseller) == START


def test_one_service_that_cannot_be_deleted_does_not_stop_the_next(
    db_session, monkeypatch, caplog
):
    first = Service(
        name="cleanup-first", service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE, config={}, product_snapshot={},
    )
    second = Service(
        name="cleanup-second", service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE, config={}, product_snapshot={},
    )
    db_session.add_all([first, second])
    db_session.commit()
    first_id, second_id = first.id, second.id
    real_delete = reseller_api.ServiceDAO.delete

    def delete(db, service_id):
        if service_id == first_id:
            raise RuntimeError("delete exploded")
        return real_delete(db, service_id)

    monkeypatch.setattr("app.api.reseller.ServiceDAO.delete", delete)
    trail = reseller_api._DeployTrail(service_ids=[first_id, second_id])

    with caplog.at_level(logging.ERROR, logger="app.api.reseller"):
        reseller_api._cleanup_failed_service(db_session, types.SimpleNamespace(id=1), trail)

    db_session.expire_all()
    assert db_session.get(Service, first_id) is not None
    assert db_session.get(Service, second_id) is None
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(f"service {first_id}" in m for m in errors), errors


# ---------------------------------------------------------------- the billing guard


def test_a_deploy_whose_billing_was_committed_is_never_refunded(
    client, db_session, monkeypatch
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "guard")
    real_finalize = ResellerBillingService.finalize_deploy
    compensations = []

    def finalize_then_fail(db, **kwargs):
        real_finalize(db, **kwargs)
        _drop_activity(db, kwargs["service"])
        raise RuntimeError("refresh exploded")

    real_compensate = ResellerBillingService.compensate_failed_deploy

    def spy(db, **kwargs):
        compensations.append(1)
        return real_compensate(db, **kwargs)

    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.finalize_deploy", finalize_then_fail
    )
    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.compensate_failed_deploy", spy
    )

    with pytest.raises(RuntimeError, match="refresh exploded"):
        _deploy(client, key, url, body)

    services = _services(db_session, body["name"])
    assert len(services) == 1
    service = services[0]
    assert db_session.query(ServiceBilling).filter_by(service_id=service.id).count() == 1
    assert compensations == []
    assert _void_invoices(db_session) == []
    assert _balance(db_session, reseller) == START - CHARGE


# ---------------------------------------------------------------- repeating the compensation


def test_compensating_twice_returns_the_charge_once(client, db_session, monkeypatch):
    reseller, key, url, body = _setup(client, db_session, "proxy", "twice")
    seen = {}

    def fail(db, *, service, charge, **_kwargs):
        seen["charge"] = charge
        _drop_activity(db, service)
        raise RuntimeError("finalize exploded")

    monkeypatch.setattr("app.api.reseller.ResellerBillingService.finalize_deploy", fail)
    with pytest.raises(RuntimeError, match="finalize exploded"):
        _deploy(client, key, url, body)
    entries = db_session.query(CreditLedgerEntry).count()
    assert _balance(db_session, reseller) == START

    ResellerBillingService.compensate_failed_deploy(
        db_session,
        reseller=reseller,
        charge=seen["charge"],
        error=RuntimeError("again"),
    )

    assert _balance(db_session, reseller) == START
    assert db_session.query(CreditLedgerEntry).count() == entries
    assert len(_void_invoices(db_session)) == 1
    request = db_session.query(ResellerProvisioningRequest).one()
    assert request.status.value == "failed"


# ---------------------------------------------------------------- the crash between the commits


def test_a_crash_between_the_commits_leaves_a_service_the_staff_check_lists(
    client, db_session, monkeypatch
):
    from app.services.unbilled_services import find_unbilled_services

    reseller, key, url, body = _setup(client, db_session, "proxy", "crash")
    _finalize_raises(monkeypatch, RuntimeError("process killed"))
    # A killed process never reaches the failure handlers: make them do nothing.
    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.compensate_failed_deploy",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(reseller_api, "_cleanup_failed_service", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="process killed"):
        _deploy(client, key, url, body)

    (service,) = _services(db_session, body["name"])
    assert db_session.query(ServiceBilling).filter_by(service_id=service.id).count() == 0
    result = find_unbilled_services(db_session, now=FAR_FUTURE)
    assert [(i.kind, i.service_id) for i in result.items] == [("reseller_client", service.id)]
    assert result.items[0].reseller_id == reseller.id
    assert result.items[0].external_service_id == body["external_service_id"]


def test_the_staff_check_is_quiet_after_a_clean_failed_deploy(
    client, db_session, monkeypatch
):
    from app.services.unbilled_services import find_unbilled_services

    reseller, key, url, body = _setup(client, db_session, "proxy", "quiet")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))

    with pytest.raises(RuntimeError):
        _deploy(client, key, url, body)

    assert find_unbilled_services(db_session, now=FAR_FUTURE).items == []
