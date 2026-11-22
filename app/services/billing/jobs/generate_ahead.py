"""``generate_invoices_ahead`` (B3, docs/billing-phase2-design.md 10.1).

A service billing due at ``next_charge_at`` with lead time ``L`` days gets its
cycle and cycle invoice at ``T = next_charge_at - L`` instead of at due. The
recurring worker's at-due path then finds and reuses that cycle: a reseller
is charged from credit at due exactly as before, a retail invoice waits to be
paid. The billing row itself is not written, so the worker's later lock of it
never meets a row changed under its snapshot.

``L`` is the ``invoice_lead_days`` of the billing's price plan cycle (found
through its product and ``cycle_interval``; when the product's plans disagree
the default applies), else ``billing_invoice_lead_days``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.reseller import BillingCycle, ServiceBilling, ServiceBillingStatus
from app.models.storefront import FrontendProduct, PricePlan, PricePlanCycle
from app.services.billing.intervals import advance_months, normalise_interval
from app.services.billing.jobs.base import JobRun, PlannedAction, _utc

NAME = "generate_invoices_ahead"
ACTION = "generate_invoice"
TRACKS_SETTING = f"billing_job_tracks_{NAME}"


def _setting(db: Session, key: str):
    from app.services.settings_store import get_setting

    return get_setting(db, key)


def subject_key(billing_id: int, due_at: datetime) -> str:
    return f"billing:{billing_id}:cycle:{_utc(due_at).isoformat()}"


def lead_days(db: Session, billing: ServiceBilling) -> int:
    default = int(_setting(db, "billing_invoice_lead_days"))
    if billing.product_id is None:
        return default
    try:
        interval = normalise_interval(billing.cycle_interval)
    except ValueError:
        return default
    values = set(
        db.scalars(
            select(PricePlanCycle.invoice_lead_days)
            .join(PricePlan, PricePlan.id == PricePlanCycle.price_plan_id)
            .join(FrontendProduct, FrontendProduct.id == PricePlan.frontend_product_id)
            .where(
                FrontendProduct.product_id == billing.product_id,
                PricePlanCycle.interval == interval,
                PricePlanCycle.invoice_lead_days.is_not(None),
            )
        )
    )
    return int(values.pop()) if len(values) == 1 else default


def _period_end(billing: ServiceBilling, due_at: datetime) -> datetime:
    from app.services.billing.renewals import billing_interval_months

    return advance_months(due_at, billing_interval_months(billing), billing.billing_anchor_day or due_at.day)


def _track_ok(db: Session, billing: ServiceBilling) -> bool:
    retail = billing.reseller_id is None
    if retail and not _setting(db, "billing_retail_renewals_enabled"):
        return False
    tracks = _setting(db, TRACKS_SETTING)
    return tracks == "both" or tracks == ("retail" if retail else "reseller")


def _cycle_exists(db: Session, billing_id: int, due_at: datetime) -> bool:
    return (
        db.scalar(
            select(BillingCycle.id).where(
                BillingCycle.service_billing_id == billing_id, BillingCycle.due_at == _utc(due_at)
            )
        )
        is not None
    )


def _blocker(db: Session, run: JobRun, billing: Optional[ServiceBilling]) -> Optional[str]:
    """Why ``billing`` must not be generated ahead now, or None."""
    if billing is None:
        return "gone"
    if billing.status != ServiceBillingStatus.ACTIVE:
        return "not_active"
    if billing.next_charge_at is None:
        return "no_due_date"
    due = _utc(billing.next_charge_at)
    if not _utc(run.now) < due:
        return "due_already"
    if billing.cancel_at is not None and _utc(billing.cancel_at) <= due:
        return "cancelled"
    if not _track_ok(db, billing):
        return "track"
    return None


class GenerateInvoicesAhead:
    name = NAME
    setting_keys = (
        "billing_invoice_lead_days",
        TRACKS_SETTING,
        "billing_retail_renewals_enabled",
    )

    def config_extra(self, db: Session) -> list:
        """Per-cycle lead times, so changing one moves the watermark (design 11.2)."""
        rows = db.execute(
            select(PricePlanCycle.id, PricePlanCycle.invoice_lead_days)
            .where(PricePlanCycle.invoice_lead_days.is_not(None))
            .order_by(PricePlanCycle.id)
        ).all()
        return [[int(row.id), int(row.invoice_lead_days)] for row in rows]

    def live_preconditions(self, db: Session) -> list[str]:
        from app.services.billing_shadow import billing_engine_mode

        if billing_engine_mode(db, "invoice") != "v2":
            return ["the invoice engine must be v2 (due and issue dates must differ)"]
        return []

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]:
        default = int(_setting(db, "billing_invoice_lead_days"))
        largest = db.scalar(select(func.max(PricePlanCycle.invoice_lead_days))) or 0
        horizon = max(default, int(largest))
        if horizon <= 0:
            return []
        now = _utc(run.now)
        return list(
            db.scalars(
                select(ServiceBilling.id)
                .where(
                    ServiceBilling.status == ServiceBillingStatus.ACTIVE,
                    ServiceBilling.next_charge_at.is_not(None),
                    ServiceBilling.next_charge_at > now,
                    ServiceBilling.next_charge_at <= now + timedelta(days=horizon),
                    or_(
                        ServiceBilling.cancel_at.is_(None),
                        ServiceBilling.cancel_at > ServiceBilling.next_charge_at,
                    ),
                    ~select(BillingCycle.id)
                    .where(
                        and_(
                            BillingCycle.service_billing_id == ServiceBilling.id,
                            BillingCycle.due_at == ServiceBilling.next_charge_at,
                        )
                    )
                    .exists(),
                )
                .order_by(ServiceBilling.next_charge_at, ServiceBilling.id)
            )
        )

    def _trigger(self, db: Session, billing: ServiceBilling) -> Optional[tuple[datetime, int]]:
        lead = lead_days(db, billing)
        if lead <= 0:
            return None
        return _utc(billing.next_charge_at) - timedelta(days=lead), lead

    def plan(self, db: Session, run: JobRun, subject_id: int) -> Optional[PlannedAction]:
        from app.services.recurring_billing_service import cycle_snapshot

        billing = db.get(ServiceBilling, subject_id)
        if _blocker(db, run, billing) is not None:
            return None
        trigger = self._trigger(db, billing)
        if trigger is None or not run.reached(trigger[0]):
            return None
        due = _utc(billing.next_charge_at)
        if _cycle_exists(db, billing.id, due):
            return None
        amount, currency, valid = cycle_snapshot(billing)
        detail = {
            "billing_id": billing.id,
            "service_id": billing.service_id,
            "reseller_id": billing.reseller_id,
            "billing_account_id": billing.billing_account_id,
            "due_at": due,
            "period_end": _period_end(billing, due),
            "lead_days": trigger[1],
            "amount_cents": amount,
            "currency": currency,
        }
        if not valid:
            detail["skip_reason"] = "invalid_billing_snapshot"
        return PlannedAction(subject_key(billing.id, due), ACTION, trigger[0], detail)

    def execute(self, db: Session, run: JobRun, action: PlannedAction) -> str:
        from app.models.reseller import BillingCycleState
        from app.services.billing.renewals import email_cycle_invoice, is_retail
        from app.services.billing_locks import BillingLocks
        from app.services.recurring_billing_service import (
            create_cycle_invoice,
            cycle_snapshot,
            lock_cycle_for_due,
        )

        billing = BillingLocks.service_billing(db, int(action.detail["billing_id"]))
        blocker = _blocker(db, run, billing)
        if blocker is not None:
            return f"skipped:{blocker}"
        due = _utc(billing.next_charge_at)
        if due != _utc(action.detail["due_at"]):
            return "skipped:due_date_changed"
        trigger = self._trigger(db, billing)
        if trigger is None or not run.in_window(trigger[0]):
            return "skipped:outside_window"
        if lock_cycle_for_due(db, billing.id, due) is not None:
            return "skipped:cycle_exists"
        amount, currency, valid = cycle_snapshot(billing)
        if not valid:
            return "skipped:invalid_billing_snapshot"
        if not run.claim(db, subject_key(billing.id, due)):
            return "already_done"
        cycle = BillingCycle(
            service_billing_id=billing.id,
            due_at=due,
            amount_cents=amount,
            currency=currency,
            state=BillingCycleState.DUE,
        )
        db.add(cycle)
        db.flush()
        invoice = create_cycle_invoice(
            db, billing, cycle, period_start=due, period_end=_period_end(billing, due)
        )
        if is_retail(billing):
            email_cycle_invoice(db, billing, invoice)
        db.flush()
        return "done"
