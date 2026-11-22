"""Shared fixtures for the P2-L job tests: a fake ``JobRun`` and retail cycle invoices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Optional

from sqlalchemy.exc import IntegrityError

from app.dao.commerce_dao import BillingAccountDAO
from app.models.billing_jobs import (
    BillingJobAction,
    BillingJobActionStatus,
    BillingNonpaymentPolicy,
    NonpaymentPolicyScope,
)
from app.models.billing_series import DocumentSeries, DocumentType
from app.models.commerce_account import SystemSetting
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


@dataclass
class FakeRun(JobRun):
    """The 14.3 run contract: ``W <= T <= now`` and a unique claim row per action."""

    claims: Optional[list] = None

    def in_window(self, trigger_at: datetime) -> bool:
        if trigger_at.tzinfo is None:
            trigger_at = trigger_at.replace(tzinfo=timezone.utc)
        if trigger_at > self.now:
            return False
        return self.watermark is None or trigger_at >= self.watermark

    def claim(self, db, subject_key: str) -> bool:
        try:
            with db.begin_nested():
                db.add(
                    BillingJobAction(
                        job=self.job,
                        subject_key=subject_key,
                        run_id=None,
                        status=BillingJobActionStatus.CLAIMED,
                        claimed_at=self.now,
                    )
                )
                db.flush()
        except IntegrityError:
            return False
        if self.claims is not None:
            self.claims.append(subject_key)
        return True


def make_run(job: str, *, now: datetime = NOW, watermark: Optional[datetime] = None) -> FakeRun:
    return FakeRun(
        run_id=1,
        job=job,
        dry_run=False,
        now=now,
        watermark=watermark if watermark is not None else now - timedelta(days=365),
        claims=[],
    )


def run_job(db, job, run) -> dict[int, str]:
    """Plan and execute every candidate once, as the runner's live mode does."""
    outcomes = {}
    for subject in list(job.candidates(db, run)):
        action = job.plan(db, run, subject)
        if action is None:
            continue
        outcomes[subject] = job.execute(db, run, action)
        db.commit()
    return outcomes


def engine_v2(db) -> None:
    set_setting(db, "billing_invoice_engine", "v2")
    if db.get(SystemSetting, "billing_engine_switch:invoice") is None:
        db.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    for code, document_type in (("CN", DocumentType.CREDIT_NOTE), ("DN", DocumentType.DEBIT_NOTE)):
        db.add(
            DocumentSeries(
                code=code, document_type=document_type, name=code, format="{SERIES}-{NUMBER}",
                number_source="own", gapless=False, reset_period="never", default_for=document_type.value,
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
