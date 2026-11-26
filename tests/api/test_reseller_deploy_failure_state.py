"""Failure handling of a reseller deploy when the database itself is failing (fabc36cb fix round).

Covers: a lost connection after the failure (the original error and the
manual-refund log line must survive), an unanswerable "was this billed?" check
(do nothing, say so), the guard retrying once on a fresh session, and gaps the
review found in the first round.
"""

import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app.api import reseller as reseller_api
from app.models.boot_task import BootTask, BootTaskStatus
from app.models.installation_task import InstallationStatus, InstallationTask
from app.models.reseller import Invoice, ServiceBilling
from app.models.service import Service
from app.services.provisioning.errors import ProvisioningError
from app.services.reseller_billing_service import ResellerBillingService
from app.services.unbilled_services import find_unbilled_services
from tests.api.test_reseller_deploy_cleanup import (
    _post_retry,
    _queue_install,
    _retry_setup,
    _server,
)
from tests.api.test_reseller_deploy_failure_paths import (
    CHARGE,
    START,
    _balance,
    _deploy,
    _drop_activity,
    _finalize_raises,
    _services,
    _setup,
    _void_invoices,
)

LOST = "Lost connection to MySQL server during query"


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def _guard_answers(monkeypatch, *answers):
    """Make the billing-guard query answer in turn; an exception instance is raised."""
    calls = []
    queue = list(answers)

    def query(session, invoice_id):
        calls.append(session)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(reseller_api, "_query_billing_committed", query)
    return calls


def _spies(monkeypatch):
    seen = {"cancel": 0, "delete": 0, "compensate": 0}
    real_cancel = reseller_api._cancel_queued_installs
    real_delete = reseller_api.ServiceDAO.delete
    real_compensate = ResellerBillingService.compensate_failed_deploy

    def cancel(db, trail):
        seen["cancel"] += 1
        return real_cancel(db, trail)

    def delete(db, service_id):
        seen["delete"] += 1
        return real_delete(db, service_id)

    def compensate(db, **kwargs):
        seen["compensate"] += 1
        return real_compensate(db, **kwargs)

    monkeypatch.setattr(reseller_api, "_cancel_queued_installs", cancel)
    monkeypatch.setattr("app.api.reseller.ServiceDAO.delete", delete)
    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.compensate_failed_deploy", compensate
    )
    return seen


# ---------------------------------------------------------------- M1: a lost connection


def _lose_connection_after_finalize(monkeypatch, db_session):
    state = {"down": False}

    def finalize(db, *, service, **_kwargs):
        _drop_activity(db, service)
        state["down"] = True
        raise RuntimeError("finalize exploded")

    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.finalize_deploy", finalize
    )

    def outage(conn, cursor, statement, parameters, context, executemany):
        if state["down"]:
            raise OperationalError(statement, parameters, Exception(LOST))

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", outage)
    return lambda: event.remove(bind, "before_cursor_execute", outage)


def test_a_lost_connection_after_the_failure_returns_the_original_error_and_logs_the_state(
    client, db_session, monkeypatch, caplog
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "outage")
    reseller_id = reseller.id
    stop = _lose_connection_after_finalize(monkeypatch, db_session)

    try:
        with caplog.at_level(logging.ERROR, logger="app.api.reseller"):
            with pytest.raises(RuntimeError, match="finalize exploded"):
                _deploy(client, key, url, body)
    finally:
        stop()

    invoice = db_session.query(Invoice).order_by(Invoice.id.desc()).first()
    assert any(
        "state unknown: check by hand" in m
        and f"reseller {reseller_id}" in m
        and f"invoice {invoice.id}" in m
        and str(CHARGE) in m
        for m in _errors(caplog)
    ), _errors(caplog)


def test_a_lost_connection_during_the_refund_logs_reseller_invoice_and_amount(
    client, db_session, monkeypatch, caplog
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "outage2")
    reseller_id = reseller.id
    _guard_answers(monkeypatch, False)
    stop = _lose_connection_after_finalize(monkeypatch, db_session)

    try:
        with caplog.at_level(logging.ERROR, logger="app.api.reseller"):
            with pytest.raises(RuntimeError, match="finalize exploded"):
                _deploy(client, key, url, body)
    finally:
        stop()

    invoice = db_session.query(Invoice).order_by(Invoice.id.desc()).first()
    assert any(
        "charge not returned: refund by hand" in m
        and f"reseller {reseller_id}" in m
        and f"invoice {invoice.id}" in m
        and str(CHARGE) in m
        for m in _errors(caplog)
    ), _errors(caplog)


# ---------------------------------------------------------------- ruling 13: state unknown


@pytest.mark.parametrize("raising", ["error", "provisioning"])
def test_when_the_billing_check_fails_twice_nothing_is_refunded_deleted_or_cancelled(
    client, db_session, monkeypatch, caplog, raising
):
    reseller, key, url, body = _setup(client, db_session, "proxy", f"unknown-{raising}")
    reseller_id = reseller.id
    exc = (
        RuntimeError("finalize exploded")
        if raising == "error"
        else ProvisioningError("boom", "provisioning refused")
    )
    _finalize_raises(monkeypatch, exc)
    calls = _guard_answers(
        monkeypatch,
        OperationalError("SELECT", {}, Exception(LOST)),
        OperationalError("SELECT", {}, Exception(LOST)),
    )
    seen = _spies(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="app.api.reseller"):
        if raising == "error":
            with pytest.raises(RuntimeError, match="finalize exploded"):
                _deploy(client, key, url, body)
        else:
            response = _deploy(client, key, url, body)
            assert 400 <= response.status_code < 500, response.text
            assert "provisioning refused" in response.text

    assert len(calls) == 2
    assert seen == {"cancel": 0, "delete": 0, "compensate": 0}
    (service,) = _services(db_session, body["name"])
    invoice = db_session.query(Invoice).order_by(Invoice.id.desc()).first()
    assert _balance(db_session, reseller) == START - CHARGE
    assert _void_invoices(db_session) == []
    line = next(m for m in _errors(caplog) if "state unknown: check by hand" in m)
    assert f"reseller {reseller_id}" in line
    assert f"invoice {invoice.id}" in line
    assert str(CHARGE) in line
    assert f"service {service.id}" in line or str(service.id) in line
    assert body["external_service_id"] in line
    assert "left queued" in line


def test_the_service_left_after_an_unknown_state_is_listed_by_the_staff_check(
    client, db_session, monkeypatch
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "unknown-list")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))
    _guard_answers(
        monkeypatch,
        OperationalError("SELECT", {}, Exception(LOST)),
        OperationalError("SELECT", {}, Exception(LOST)),
    )

    with pytest.raises(RuntimeError):
        _deploy(client, key, url, body)

    (service,) = _services(db_session, body["name"])
    service.created_at = datetime.utcnow() - timedelta(hours=2)
    db_session.commit()
    result = find_unbilled_services(db_session)
    assert [(i.kind, i.service_id) for i in result.items] == [("reseller_client", service.id)]


def test_a_billing_check_that_fails_once_then_says_billed_refunds_nothing(
    client, db_session, monkeypatch
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "once-billed")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))
    _guard_answers(monkeypatch, OperationalError("SELECT", {}, Exception(LOST)), True)
    seen = _spies(monkeypatch)

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _deploy(client, key, url, body)

    assert seen == {"cancel": 0, "delete": 0, "compensate": 0}
    assert len(_services(db_session, body["name"])) == 1
    assert _void_invoices(db_session) == []
    assert _balance(db_session, reseller) == START - CHARGE


def test_a_billing_check_that_fails_once_then_says_not_billed_compensates_and_cleans_up(
    client, db_session, monkeypatch
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "once-unbilled")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))
    _guard_answers(monkeypatch, OperationalError("SELECT", {}, Exception(LOST)), False)

    with pytest.raises(RuntimeError, match="finalize exploded"):
        _deploy(client, key, url, body)

    assert _services(db_session, body["name"]) == []
    assert _balance(db_session, reseller) == START
    assert len(_void_invoices(db_session)) == 1


def test_the_retry_uses_a_fresh_session_from_the_current_session_factory_and_closes_it(
    client, db_session, monkeypatch
):
    from app.core import database

    reseller, key, url, body = _setup(client, db_session, "proxy", "fresh")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))
    calls = _guard_answers(monkeypatch, OperationalError("SELECT", {}, Exception(LOST)), False)
    made = []
    real_factory = database.SessionLocal

    def factory():
        session = real_factory()
        closed = []
        real_close = session.close
        session.close = lambda: (closed.append(1), real_close())[1]
        made.append((session, closed))
        return session

    monkeypatch.setattr(database, "SessionLocal", factory)

    with pytest.raises(RuntimeError):
        _deploy(client, key, url, body)

    assert len(made) == 1
    fresh, closed = made[0]
    assert calls[1] is fresh
    assert calls[0] is not fresh
    assert closed == [1]


# ---------------------------------------------------------------- L1 to L4


def test_a_billed_bare_metal_deploy_keeps_its_install_and_boot_tasks_pending(
    client, db_session, monkeypatch
):
    key, body, retried_id = _retry_setup(client, db_session, "fp-l1")
    server_id = _server(db_session)
    queued = {}

    def resume(db, service, req, actor):
        queued["ids"] = _queue_install(db, server_id)
        return service

    monkeypatch.setattr(
        "app.services.provisioning.service.resume_bare_metal_order", resume
    )
    real_finalize = ResellerBillingService.finalize_deploy

    def finalize_then_fail(db, **kwargs):
        real_finalize(db, **kwargs)
        raise RuntimeError("refresh exploded")

    monkeypatch.setattr(
        "app.api.reseller.ResellerBillingService.finalize_deploy", finalize_then_fail
    )

    with pytest.raises(RuntimeError, match="refresh exploded"):
        _post_retry(client, key, body, "fp-l1-deploy")

    db_session.expire_all()
    boot_id, task_id = queued["ids"]
    assert db_session.query(ServiceBilling).filter_by(service_id=retried_id).count() == 1
    assert db_session.get(InstallationTask, task_id).status == InstallationStatus.PENDING
    assert db_session.get(BootTask, boot_id).status == BootTaskStatus.PENDING


def test_a_failed_install_cancellation_is_logged_at_error_level(
    client, db_session, monkeypatch, caplog
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "l2")
    _finalize_raises(monkeypatch, RuntimeError("finalize exploded"))

    def cancel_fails(db, trail):
        raise RuntimeError("cancel exploded")

    monkeypatch.setattr(reseller_api, "_cancel_queued_installs", cancel_fails)

    with caplog.at_level(logging.WARNING, logger="app.api.reseller"):
        with pytest.raises(RuntimeError, match="finalize exploded"):
            _deploy(client, key, url, body)

    records = [r for r in caplog.records if "cancel queued installs" in r.getMessage()]
    assert records and all(r.levelno >= logging.ERROR for r in records)


def test_billing_that_is_not_linked_to_this_invoice_does_not_count_as_committed(
    client, db_session, monkeypatch
):
    reseller, key, url, body = _setup(client, db_session, "proxy", "l3")
    seen = {}

    def fail(db, *, service, charge, **_kwargs):
        seen["service_id"] = service.id
        seen["invoice_id"] = charge.invoice.id
        _drop_activity(db, service)
        raise RuntimeError("finalize exploded")

    monkeypatch.setattr("app.api.reseller.ResellerBillingService.finalize_deploy", fail)
    monkeypatch.setattr(reseller_api, "_cleanup_failed_service", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        _deploy(client, key, url, body)

    db_session.add(ServiceBilling(service_id=seen["service_id"], monthly_price_cents=2000))
    db_session.commit()
    db_session.expire_all()
    invoice = db_session.get(Invoice, seen["invoice_id"])
    assert invoice.service_id != seen["service_id"]

    assert reseller_api._deploy_billing_committed(db_session, invoice.id) is False
    invoice.service_id = seen["service_id"]
    db_session.commit()
    assert reseller_api._deploy_billing_committed(db_session, invoice.id) is True


def test_an_owner_less_service_with_a_recurring_order_item_is_not_listed(db_session):
    from tests.services.test_unbilled_services import _find, _order_item, _service, _user

    service = _service(db_session, None)
    # The order item needs an account owner; the service itself stays owner-less.
    _order_item(db_session, service, _user(db_session).id)

    assert _find(db_session).items == []
