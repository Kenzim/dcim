"""Shared helpers for the P2-L job tests: a database per test, runs through the real runner, retail cycle invoices."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Iterator, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.dao.commerce_dao import BillingAccountDAO
from app.models.billing_jobs import (
    BILLING_JOB_NAMES,
    BillingJobMode,
    BillingJobRunItem,
    BillingJobState,
    BillingNonpaymentPolicy,
    NonpaymentPolicyScope,
)
from app.models.billing_series import DocumentSeries, DocumentType
from app.models.commerce_account import SystemSetting
from app.models.commerce_email import EmailTemplate
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User
from app.services.billing.jobs.base import JobRun
from app.services.settings_store import set_setting

NOW = datetime(2027, 3, 20, 12, 0, tzinfo=timezone.utc)
_numbers = count(91000)
_names = count(1)


# The email templates the P2-L jobs need enabled to go live.
JOB_TEMPLATES = (
    "late_fee_added",
    "service_suspended_nonpayment",
    "service_termination_warning",
    "service_terminated",
    "cancellation_completed",
)


def job_database(tmp_path, monkeypatch) -> Iterator[Session]:
    """A file SQLite database of its own, so the runner's sessions are separate connections.

    Every job has its state row (off). ``SessionLocal`` points at it for the
    code that opens its own sessions (number counters, post-commit lifecycle
    calls). Yields the test's session.
    """
    import app.core.database as database
    from app.core.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'p2l.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    db = factory()
    for name in BILLING_JOB_NAMES:
        db.add(BillingJobState(job=name))
    db.commit()
    enable_stuck_service_alerts(db)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def enable_stuck_service_alerts(db, *keys: str, enabled: bool = True) -> None:
    """Turn the stuck-service alerts on (all, or ``keys``): the non-payment jobs' live precondition."""
    from app.models.alerting import AlertTypeSetting
    from app.services.billing.jobs.nonpayment import STUCK_SERVICE_ALERTS

    for key in keys or STUCK_SERVICE_ALERTS:
        row = db.get(AlertTypeSetting, key) or AlertTypeSetting(alert_type=key, params={})
        row.enabled = enabled
        db.add(row)
    db.commit()


def enable_templates(db, *events: str, enabled: bool = True) -> None:
    """Give each event a default template (``enabled`` as given); all the P2-L job templates by default."""
    for event in events or JOB_TEMPLATES:
        row = db.query(EmailTemplate).filter_by(event_key=event, product_scope=0).one_or_none()
        if row is None:
            row = EmailTemplate(event_key=event, subject=f"{event} subject", body_text=f"{event} body")
            db.add(row)
        row.enabled = enabled
    db.commit()


def make_run(
    job: str, *, now: datetime = NOW, watermark: Optional[datetime] = None, catch_up_days: int = 30
) -> JobRun:
    """A live run handle for calling ``plan`` and ``execute`` directly (claims have no run row)."""
    return JobRun(
        run_id=None,
        job=job,
        dry_run=False,
        now=now,
        watermark=watermark if watermark is not None else now - timedelta(days=365),
        catch_up_days=catch_up_days,
    )


def run_items(db, run_id: int) -> dict[str, str]:
    """``{subject_key: outcome}`` of a run; a skip reads ``skipped:<reason>``."""
    items = {}
    for item in db.query(BillingJobRunItem).filter_by(run_id=run_id).order_by(BillingJobRunItem.id):
        outcome = item.outcome.value
        reason = (item.detail or {}).get("skip_reason")
        items[item.subject_key] = f"{outcome}:{reason}" if outcome == "skipped" and reason else outcome
    return items


def run_job(
    db, job, run: JobRun, *, arm: bool = True, status: str = "complete", dry_run: bool = False
) -> dict[str, str]:
    """Run ``job`` once (live unless ``dry_run``) through ``runner.run_job`` at ``run.now``; returns ``run_items``.

    With ``arm`` the job's state is set live with ``live_since`` and the
    recorded config at ``run.watermark``, so the run's watermark is
    ``run.watermark`` whatever changed before; without it the state is used as
    it is. ``job`` (with its test lifecycle) stands in for the registered job
    for the run. The run must end with ``status``.
    """
    from app.services.billing.jobs import registry, runner
    from app.services.settings_store import set_setting

    set_setting(db, "billing_job_max_catch_up_days", run.catch_up_days)
    if arm:
        state = db.get(BillingJobState, job.name)
        state.mode = BillingJobMode.LIVE
        state.live_since = run.watermark
        state.dry_run_since = None
        state.config_hash = runner.config_hash(db, job)
        state.config_since = run.watermark
    db.commit()
    registered = registry.JOBS[job.name]
    registry.JOBS[job.name] = job
    try:
        result = runner.run_job(
            sessionmaker(bind=db.get_bind(), autoflush=False), job.name, dry_run=dry_run, trigger="admin", now=run.now
        )
    finally:
        registry.JOBS[job.name] = registered
    assert result.status == status, result
    db.expire_all()
    return run_items(db, result.run_id) if result.run_id is not None else {}


def go_live(db, job, at: datetime) -> None:
    """Take ``job`` from off to dry_run (one scheduled dry run) and live at ``at``, as an operator would."""
    from app.services.billing.jobs import registry, runner

    registered = registry.JOBS[job.name]
    registry.JOBS[job.name] = job
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    try:
        for to, when in (("dry_run", at - timedelta(days=1)), ("live", at)):
            with factory() as session:
                runner.change_mode(session, job.name, to, now=when, actor_user_id=None)
                session.commit()
            if to == "dry_run":
                result = runner.run_job(factory, job.name, dry_run=True, trigger="schedule", now=when)
                assert result.status == "complete", result
    finally:
        registry.JOBS[job.name] = registered
    db.expire_all()


def refused_live(db, job, at: datetime) -> str:
    """Take ``job`` from off to dry_run and run a scheduled dry run (both must succeed), then ask for live.

    Returns the refusal message; the job must stay in dry_run.
    """
    from app.services.billing.jobs import registry, runner

    registered = registry.JOBS[job.name]
    registry.JOBS[job.name] = job
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    try:
        with factory() as session:
            runner.change_mode(session, job.name, "dry_run", now=at - timedelta(days=1), actor_user_id=None)
            session.commit()
        result = runner.run_job(factory, job.name, dry_run=True, trigger="schedule", now=at - timedelta(days=1))
        assert result.status == "complete", result
        with factory() as session:
            try:
                runner.change_mode(session, job.name, "live", now=at, actor_user_id=None)
            except runner.JobModeError as exc:
                session.rollback()
                message = str(exc)
            else:
                raise AssertionError("the job went live")
    finally:
        registry.JOBS[job.name] = registered
    db.expire_all()
    assert db.get(BillingJobState, job.name).mode == BillingJobMode.DRY_RUN
    return message


def engine_v2(db) -> None:
    set_setting(db, "billing_invoice_engine", "v2")
    if db.get(SystemSetting, "billing_engine_switch:invoice") is None:
        db.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    for code, document_type in (
        ("INV", DocumentType.INVOICE),
        ("CN", DocumentType.CREDIT_NOTE),
        ("DN", DocumentType.DEBIT_NOTE),
    ):
        db.add(
            DocumentSeries(
                code=code, document_type=document_type, name=code, format="{SERIES}-{NUMBER}",
                number_source="own", gapless=document_type == DocumentType.INVOICE, reset_period="never", default_for=document_type.value,
            )
        )
    db.commit()


def make_account(db, name: Optional[str] = None):
    name = name or f"p2l-client-{next(_names)}"
    user = User(username=name, email=f"{name}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    return BillingAccountDAO.ensure_client_account(db, user.id)


def make_reseller(db):
    from app.models.reseller import Reseller

    tag = next(_names)
    reseller = Reseller(user=User(username=f"p2l-rsl-{tag}", email=f"p2l-rsl-{tag}@example.com", is_reseller=True))
    db.add(reseller)
    db.commit()
    return reseller


def make_service(db, account, *, status=ServiceStatus.ACTIVE, reseller_id=None) -> Service:
    service = Service(
        name=f"p2l-service-{next(_names)}",
        service_type=ServiceType.VM,
        status=status,
        owner_user_id=account.user_id if account is not None else None,
    )
    db.add(service)
    db.commit()
    return service


def make_billing(db, account, service, *, reseller_id=None, status=ServiceBillingStatus.ACTIVE, **values) -> ServiceBilling:
    billing = ServiceBilling(
        service_id=service.id,
        reseller_id=reseller_id,
        billing_account_id=account.id if account is not None and reseller_id is None else None,
        monthly_price_cents=values.pop("monthly_price_cents", 1000),
        currency=values.pop("currency", "USD"),
        status=status,
        next_charge_at=values.pop("next_charge_at", NOW + timedelta(days=20)),
        **values,
    )
    db.add(billing)
    db.commit()
    return billing


def make_cycle_invoice(
    db,
    billing: ServiceBilling,
    *,
    due_at: datetime,
    amount: int = 1000,
    status: InvoiceStatus = InvoiceStatus.OVERDUE,
    currency: str = "USD",
) -> Invoice:
    invoice = Invoice(
        invoice_number=next(_numbers),
        billing_account_id=billing.billing_account_id,
        reseller_id=billing.reseller_id,
        service_id=billing.service_id,
        purpose=InvoicePurpose.CYCLE_CHARGE,
        status=status,
        amount_cents=amount,
        currency=currency,
        due_at=due_at,
    )
    db.add(invoice)
    db.flush()
    db.add(
        BillingCycle(
            service_billing_id=billing.id,
            invoice_id=invoice.id,
            due_at=due_at,
            amount_cents=amount,
            currency=currency,
            state=BillingCycleState.PENDING_ACTION,
        )
    )
    db.commit()
    return invoice


def set_policy(db, *, scope=NonpaymentPolicyScope.RETAIL_DEFAULT, product_id=0, **values) -> BillingNonpaymentPolicy:
    row = (
        db.query(BillingNonpaymentPolicy)
        .filter_by(scope=scope, product_id=product_id)
        .one_or_none()
    )
    if row is None:
        row = BillingNonpaymentPolicy(scope=scope, product_id=product_id)
        db.add(row)
    row.enabled = values.pop("enabled", True)
    for key, value in values.items():
        setattr(row, key, value)
    db.commit()
    return row
