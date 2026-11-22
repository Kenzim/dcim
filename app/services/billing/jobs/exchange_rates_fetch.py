"""``tax_exchange_rates_fetch`` (docs/billing-phase2-design.md 4.10 and 9.1).

Daily, stores HMRC's monthly rates for the current and the next UK month
when the operator currency's rate for that month is not stored yet. The
rates come from the ``currency_rates`` provider ``PROVIDER_REF`` (the bundled
``hmrc_exchange_rates`` plugin). Each month is stored once: a currency already
stored is never changed, and a different value offered for it is recorded on
the action, which is left ``needs_review`` so ``billing_job_needs_review``
raises it. A month the source has not published yet is skipped and asked for
again on the next run. Publishing reads only the stored table.

A month whose operator-currency rate an administrator entered by hand
(``hand_entered``, any month) is fetched once more to compare that rate with
the source's file: a difference is the same conflict (``needs_review``,
audited, never overwritten). Either way the rate's ``compared_at`` is set, so
the month is not fetched again; a month not published yet is asked for again.

The provider call is a read outside the database: the item's read transaction
is ended before it, and the rows are written and claimed after it in one
transaction. Subjects are months as ``YYYYMM`` numbers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.billing.jobs.base import JobRun, PlannedAction, _utc

NAME = "tax_exchange_rates_fetch"
ACTION = "fetch_exchange_rates"
COMPARE_ACTION = "compare_exchange_rate"
PROVIDER_KIND = "currency_rates"
PROVIDER_REF = "hmrc_exchange_rates:hmrc_monthly"
MONTH_OFFSETS = (0, 1)


def period_for(run: JobRun, offset: int) -> str:
    from app.services.tax.exchange_rates import next_period, period_of

    period = period_of(_utc(run.now))
    for _ in range(offset):
        period = next_period(period)
    return period


def subject_of(period: str) -> int:
    year, month = period.split("-")
    return int(year) * 100 + int(month)


def period_of_subject(subject_id: int) -> str:
    return f"{int(subject_id) // 100:04d}-{int(subject_id) % 100:02d}"


def subject_key(period: str, currency: str) -> str:
    return f"period:{period}:{currency}"


def compare_key(period: str, currency: str) -> str:
    return f"compare:{period}:{currency}"


def _missing(db: Session, period: str, currency: str) -> bool:
    from app.services.tax.exchange_rates import SOURCE_HMRC, has_rate

    return not has_rate(db, currency, period, SOURCE_HMRC)


def _uncompared(db: Session, currency: str, period: Optional[str] = None):
    """Hand-entered operator-currency rates not compared with the source yet."""
    from app.models.tax import TaxExchangeRate
    from app.services.tax.exchange_rates import BASE, SOURCE_HMRC

    stmt = select(TaxExchangeRate).where(
        TaxExchangeRate.source == SOURCE_HMRC,
        TaxExchangeRate.base == BASE,
        TaxExchangeRate.currency == currency,
        TaxExchangeRate.hand_entered.is_(True),
        TaxExchangeRate.compared_at.is_(None),
    )
    if period is not None:
        stmt = stmt.where(TaxExchangeRate.period == period)
    return db.scalars(stmt.order_by(TaxExchangeRate.period))


def _provider_available() -> bool:
    from app.plugin_host.providers import providers_of

    try:
        return any(entry.ref == PROVIDER_REF for entry in providers_of(PROVIDER_KIND))
    except NotImplementedError:
        return False


def _audit_conflicts(db: Session, period: str, conflicts) -> None:
    from app.services.audit_service import AuditService

    AuditService.log(
        db,
        action="billing.tax.exchange_rate_conflict",
        resource_type="tax_exchange_rate",
        resource_id=period,
        metadata={
            "period": period,
            "conflicts": [
                {"currency": code, "stored": str(stored), "offered": str(offered)}
                for code, stored, offered in conflicts
            ],
        },
    )


def _mark_compared(db: Session, period: str, currency: str, at: datetime) -> None:
    for row in _uncompared(db, currency, period):
        row.compared_at = at
    db.flush()


class TaxExchangeRatesFetch:
    name = NAME
    setting_keys: tuple[str, ...] = ()

    def config_extra(self, db: Session) -> dict:
        from app.services.billing.operator_currency import operator_currency

        return {"operator_currency": operator_currency(), "provider": PROVIDER_REF}

    def live_preconditions(self, db: Session) -> list[str]:
        if _provider_available():
            return []
        return [f"the currency_rates provider {PROVIDER_REF} is not installed and enabled"]

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]:
        from app.services.billing.operator_currency import operator_currency

        currency = operator_currency()
        if currency == "GBP":
            return []
        periods = {period_for(run, offset) for offset in MONTH_OFFSETS}
        wanted = {period for period in periods if _missing(db, period, currency)}
        wanted |= {row.period for row in _uncompared(db, currency)}
        return sorted(subject_of(period) for period in wanted)

    def plan(self, db: Session, run: JobRun, subject_id: int) -> Optional[PlannedAction]:
        from app.services.billing.operator_currency import operator_currency

        currency = operator_currency()
        period = period_of_subject(subject_id)
        if currency == "GBP":
            return None
        detail = {"period": period, "currency": currency, "provider": PROVIDER_REF}
        if _missing(db, period, currency):
            return PlannedAction(subject_key(period, currency), ACTION, _utc(run.now), detail)
        if next(iter(_uncompared(db, currency, period)), None) is not None:
            return PlannedAction(compare_key(period, currency), COMPARE_ACTION, _utc(run.now), detail)
        return None

    def execute(self, db: Session, run: JobRun, action: PlannedAction) -> str:
        from app.plugin_host.kinds import CurrencyRatesRequest
        from app.plugin_host.providers import ProviderUnavailable, call_provider
        from app.services.tax import exchange_rates

        period = str(action.detail["period"])
        currency = str(action.detail["currency"])
        comparing = action.action == COMPARE_ACTION
        if not run.in_window(action.trigger_at):
            return "skipped:not_due"
        if not self._still_wanted(db, period, currency, comparing):
            return "skipped:already_stored"
        # No transaction is open across the provider call.
        db.rollback()
        try:
            answer = call_provider(
                PROVIDER_KIND, PROVIDER_REF, "rates", CurrencyRatesRequest(base=exchange_rates.BASE, period=period)
            )
        except ProviderUnavailable as exc:
            return f"failed:provider_{exc.reason or 'unavailable'}"
        offered = {rate.currency: rate.units_per_base for rate in answer.rates}
        if currency not in offered:
            return "skipped:not_published"

        key = action.subject_key
        if not self._still_wanted(db, period, currency, comparing):
            return "skipped:already_stored"
        if not run.claim(db, key):
            return "already_done"
        outcome = exchange_rates.store_month(
            db, period, sorted(offered.items()), source=answer.source, fetched_at=_utc(run.now)
        )
        if comparing:
            _mark_compared(db, period, currency, _utc(run.now))
        detail = {
            "period": period,
            "source": answer.source,
            "inserted": len(outcome.inserted),
            "unchanged": len(outcome.unchanged),
        }
        if comparing:
            detail["compared"] = currency
        if outcome.conflicts:
            detail["conflicts"] = [
                {"currency": code, "stored": str(stored), "offered": str(offered_rate)}
                for code, stored, offered_rate in outcome.conflicts
            ]
            _audit_conflicts(db, period, outcome.conflicts)
            run.finish(db, key, "needs_review", detail)
            return "done:conflict"
        run.finish(db, key, "done", detail)
        return "done"

    @staticmethod
    def _still_wanted(db: Session, period: str, currency: str, comparing: bool) -> bool:
        if comparing:
            return next(iter(_uncompared(db, currency, period)), None) is not None
        return _missing(db, period, currency)
